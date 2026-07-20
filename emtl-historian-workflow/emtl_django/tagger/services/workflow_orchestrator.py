from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from tagger.models import Document, StageOutput
from tagger.stage_config import STAGE_PROFILES, dependency_closure, ordered_stage_ids

from .entity_review_handoff import entity_downstream_is_eligible


CONTRACT_VERSION = "recoverable-workflow-orchestrator-v1"


@dataclass(frozen=True)
class OrchestrationPlan:
    contract_version: str
    document_id: str
    requested_stages: list[str]
    expanded_stages: list[str]
    auto_included_stages: list[str]
    steps: list[dict[str, Any]]
    next_stage_id: str
    next_action: str
    state: str
    plan_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "document_id": self.document_id,
            "requested_stages": self.requested_stages,
            "expanded_stages": self.expanded_stages,
            "auto_included_stages": self.auto_included_stages,
            "steps": self.steps,
            "next_stage_id": self.next_stage_id,
            "next_action": self.next_action,
            "state": self.state,
            "plan_fingerprint": self.plan_fingerprint,
        }


def build_orchestration_plan(
    *, document: Document, requested_stages: set[str], stage_outputs: dict[str, StageOutput] | None = None
) -> OrchestrationPlan:
    expanded = dependency_closure(requested_stages)
    requested_ordered = ordered_stage_ids(requested_stages)
    expanded_ordered = ordered_stage_ids(expanded)
    outputs = stage_outputs or {row.stage: row for row in document.stage_outputs.all()}
    reusable: set[str] = set()
    steps: list[dict[str, Any]] = []
    for stage_id in expanded_ordered:
        output = outputs.get(stage_id)
        dependencies = list(STAGE_PROFILES[stage_id].requires)
        missing = [item for item in dependencies if item not in reusable]
        if _is_reusable(stage_id, output):
            action = "reuse_accepted"
            reusable.add(stage_id)
        elif missing:
            action = "wait_for_dependencies"
        elif output is None or output.status in {StageOutput.Status.NOT_STARTED, StageOutput.Status.LOADED}:
            action = "run"
        elif output.status == StageOutput.Status.CHECKING:
            action = "await_human_review"
        elif output.status == StageOutput.Status.NEEDS_RERUN:
            action = "rerun"
        elif output.status == StageOutput.Status.BLOCKED:
            action = "retry_after_dependency_recheck"
        else:
            action = "run"
        steps.append({
            "stage_id": stage_id,
            "label": STAGE_PROFILES[stage_id].label,
            "requested_by_user": stage_id in requested_stages,
            "auto_included_dependency": stage_id not in requested_stages,
            "requires": dependencies,
            "missing_reusable_dependencies": missing,
            "stage_output_id": output.pk if output else None,
            "stage_status": output.status if output else StageOutput.Status.NOT_STARTED,
            "action": action,
        })
    next_step = next((step for step in steps if step["action"] != "reuse_accepted"), None)
    if next_step is None:
        state, next_stage, next_action = "complete", "", "complete"
    else:
        next_stage, next_action = next_step["stage_id"], next_step["action"]
        state = "waiting_for_review" if next_action == "await_human_review" else "ready"
        if next_action == "wait_for_dependencies":
            state = "blocked"
    fingerprint_source = {
        "document_id": document.doc_id,
        "requested": requested_ordered,
        "expanded": expanded_ordered,
        "steps": [(row["stage_id"], row["stage_output_id"], row["stage_status"], row["action"]) for row in steps],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return OrchestrationPlan(
        contract_version=CONTRACT_VERSION,
        document_id=document.doc_id,
        requested_stages=requested_ordered,
        expanded_stages=expanded_ordered,
        auto_included_stages=[item for item in expanded_ordered if item not in requested_stages],
        steps=steps,
        next_stage_id=next_stage,
        next_action=next_action,
        state=state,
        plan_fingerprint=fingerprint,
    )


@transaction.atomic
def resume_orchestration(*, document: Document, requested_stages: set[str]) -> OrchestrationPlan:
    locked = Document.objects.select_for_update().get(pk=document.pk)
    outputs = {row.stage: row for row in locked.stage_outputs.all()}
    plan = build_orchestration_plan(
        document=locked, requested_stages=requested_stages, stage_outputs=outputs
    )
    metadata = dict(locked.metadata or {})
    previous = dict(metadata.get("workflow_orchestrator") or {})
    snapshot = plan.as_dict()
    snapshot.update({
        "updated_at": timezone.now().isoformat(),
        "resume_count": int(previous.get("resume_count") or 0) + 1,
        "previous_plan_fingerprint": previous.get("plan_fingerprint", ""),
    })
    metadata["workflow_orchestrator"] = snapshot
    locked.metadata = metadata
    locked.save(update_fields=["metadata", "updated_at"])
    return plan


def _is_reusable(stage_id: str, output: StageOutput | None) -> bool:
    if output is None or output.status != StageOutput.Status.ACCEPTED:
        return False
    if stage_id == StageOutput.Stage.ENTITY_REGISTRY:
        return entity_downstream_is_eligible(output)
    return True
