from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from tagger.models import NewIdProposal, StageOutput


REVIEW_CONTRACT = "entity-human-review-v1"
DOWNSTREAM_CONTRACT = "entity-reviewed-downstream-v1"
DECISIONS = {"accepted", "edited", "rejected"}
ENTITY_RECORD_TYPES = {"P", "PF", "R", "L", "I", "T", "INT", "C", "TE"}


def export_entity_review_file(*, stage_output: StageOutput, output_path: Path) -> dict[str, Any]:
    payload = stage_output.payload or {}
    entity_output = payload.get("entity_output") or {}
    rows = entity_output.get("tags") or []
    review = payload.get("entity_review") or {}
    if stage_output.status != StageOutput.Status.CHECKING or review.get("state") != "review_candidate":
        raise ValueError("Entity StageOutput is not a checking review candidate.")
    attempt_id = review.get("attempt_id")
    exported_rows = []
    for index, original in enumerate(rows, start=1):
        original_copy = copy.deepcopy(original)
        exported_rows.append({
            "review_row_id": f"entity-{index:04d}",
            "decision": "pending",
            "original_row": original_copy,
            "edited_row": copy.deepcopy(original_copy),
            "provenance": {
                "source_attempt_id": attempt_id,
                "original_row_index": index,
                "original_raw_line": original.get("raw_line", "") if isinstance(original, dict) else "",
            },
        })
    document = {
        "contract_version": REVIEW_CONTRACT,
        "stage_output_id": stage_output.pk,
        "source_attempt_id": attempt_id,
        "raw_output_sha256": hashlib.sha256(stage_output.raw_output.encode("utf-8")).hexdigest(),
        "instructions": "Set every decision to accepted, edited, or rejected. Change edited_row only for edited decisions.",
        "rows": exported_rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


@transaction.atomic
def import_entity_review_file(
    *, stage_output: StageOutput, input_path: Path, confirm_approve_for_downstream: bool,
    accept_remaining: bool = False,
) -> dict[str, Any]:
    if not confirm_approve_for_downstream:
        raise ValueError("Explicit downstream approval confirmation is required.")
    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    if locked.status != StageOutput.Status.CHECKING:
        raise ValueError("Entity StageOutput must be checking before review import.")
    review_document = json.loads(input_path.read_text(encoding="utf-8"))
    if accept_remaining:
        review_document, _ = accept_remaining_entity_review_rows(review_document)
    payload = copy.deepcopy(locked.payload or {})
    current_review = payload.get("entity_review") or {}
    original_rows = (payload.get("entity_output") or {}).get("tags") or []
    if review_document.get("contract_version") != REVIEW_CONTRACT:
        raise ValueError("Unsupported Entity review contract.")
    if review_document.get("stage_output_id") != locked.pk:
        raise ValueError("Review file belongs to a different StageOutput.")
    if review_document.get("source_attempt_id") != current_review.get("attempt_id"):
        raise ValueError("Review file source attempt does not match the review candidate.")
    if review_document.get("raw_output_sha256") != hashlib.sha256(locked.raw_output.encode("utf-8")).hexdigest():
        raise ValueError("Review file raw-output fingerprint does not match.")
    file_rows = review_document.get("rows")
    if not isinstance(file_rows, list) or len(file_rows) != len(original_rows):
        raise ValueError("Review file row count does not match the original parsed rows.")

    reviewed_rows = []
    downstream_rows = []
    for index, (file_row, original) in enumerate(zip(file_rows, original_rows), start=1):
        if not isinstance(file_row, dict) or file_row.get("review_row_id") != f"entity-{index:04d}":
            raise ValueError(f"Invalid review row identity at row {index}.")
        if file_row.get("original_row") != original:
            raise ValueError(f"Original row {index} was modified.")
        decision = str(file_row.get("decision") or "").strip().lower()
        if decision not in DECISIONS:
            raise ValueError(f"Row {index} requires accepted, edited, or rejected decision.")
        selected = copy.deepcopy(original if decision == "accepted" else file_row.get("edited_row"))
        if decision == "edited" and not isinstance(selected, dict):
            raise ValueError(f"Edited row {index} requires an edited_row object.")
        provenance = {
            "source_attempt_id": current_review.get("attempt_id"),
            "original_row_index": index,
            "original_raw_line": original.get("raw_line", "") if isinstance(original, dict) else "",
        }
        reviewed_rows.append({
            "review_row_id": f"entity-{index:04d}",
            "decision": decision,
            "original_row": copy.deepcopy(original),
            "reviewed_row": selected if decision != "rejected" else None,
            "provenance": provenance,
        })
        if decision != "rejected":
            downstream_rows.append({**selected, "review_provenance": provenance, "review_decision": decision})

    reviewed_at = timezone.now().isoformat()
    payload["reviewed_entity_registry"] = downstream_rows
    payload["entity_review"] = {
        **current_review,
        "contract_version": REVIEW_CONTRACT,
        "state": "approved",
        "approved_for_downstream": True,
        "reviewed_at": reviewed_at,
        "review_rows": reviewed_rows,
        "accepted_or_edited_count": len(downstream_rows),
        "rejected_count": len(reviewed_rows) - len(downstream_rows),
    }
    provenance = copy.deepcopy(locked.provenance or {})
    entity_provenance = dict(provenance.get("entity_registry") or {})
    entity_provenance.update({"approved_for_downstream": True, "reviewed_at": reviewed_at})
    provenance["entity_registry"] = entity_provenance
    locked.payload = payload
    locked.provenance = provenance
    locked.status = StageOutput.Status.ACCEPTED
    locked.save(update_fields=["payload", "provenance", "status", "updated_at"])
    return build_entity_downstream_package(locked)


def accept_remaining_entity_review_rows(
    review_document: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Accept pending rows while preserving explicit edits and rejects."""
    updated = copy.deepcopy(review_document)
    rows = updated.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Entity review rows must be a list.")
    accepted_count = 0
    for row in rows:
        if isinstance(row, dict) and str(row.get("decision") or "").strip().lower() == "pending":
            row["decision"] = "accepted"
            accepted_count += 1
    return updated, accepted_count


def build_entity_downstream_package(stage_output: StageOutput) -> dict[str, Any]:
    payload = stage_output.payload or {}
    review = payload.get("entity_review") or {}
    provenance = stage_output.provenance or {}
    entity_provenance = provenance.get("entity_registry") or {}
    if not entity_downstream_is_eligible(stage_output):
        raise ValueError("Entity output is not explicitly approved for downstream use.")
    validation = payload.get("entity_validation") or {}
    reviewed_rows = copy.deepcopy(payload.get("reviewed_entity_registry") or [])
    reviewed_rows.extend(_approved_user_entity_rows(stage_output))
    return {
        "contract_version": DOWNSTREAM_CONTRACT,
        "stage_output_id": stage_output.pk,
        "source_attempt_id": review.get("attempt_id"),
        "registry_version": validation.get("registry_version", ""),
        "registry_hashes": validation.get("resource_hashes", {}),
        "review_state": review.get("state"),
        "reviewed_at": review.get("reviewed_at"),
        "reviewed_rows": reviewed_rows,
    }


@transaction.atomic
def propose_entity(
    *, stage_output: StageOutput, record_type: str, headword: str,
    evidence_form: str = "", reviewer_note: str = "", source_clause=None,
) -> NewIdProposal:
    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    if locked.stage != StageOutput.Stage.ENTITY_REGISTRY:
        raise ValueError("Entity proposals require an Entity Registry StageOutput.")
    normalized_type = str(record_type or "").strip().upper()
    normalized_headword = " ".join(str(headword or "").split())
    if normalized_type not in ENTITY_RECORD_TYPES:
        raise ValueError("Unsupported Entity record type.")
    if not normalized_headword:
        raise ValueError("Entity headword is required.")
    prefix = f"NEW-{normalized_type}-"
    sequence = 1
    existing = NewIdProposal.objects.filter(
        document=locked.document, proposed_id__startswith=prefix
    ).values_list("proposed_id", flat=True)
    used = {value for value in existing}
    while f"{prefix}{sequence:04d}" in used:
        sequence += 1
    proposed_id = f"{prefix}{sequence:04d}"
    proposal = NewIdProposal.objects.create(
        document=locked.document,
        source_clause=source_clause,
        proposed_id=proposed_id,
        record_type=normalized_type,
        headword=normalized_headword,
        evidence_form=str(evidence_form or "").strip(),
        reviewer_note=str(reviewer_note or "").strip(),
        payload={
            "contract_version": "entity-user-proposal-v1",
            "proposal_kind": "entity_user_proposal",
            "source_stage_output_id": locked.pk,
        },
    )
    payload = copy.deepcopy(locked.payload or {})
    review = dict(payload.get("entity_review") or {})
    review.update({"state": "review_candidate", "approved_for_downstream": False})
    payload["entity_review"] = review
    provenance = copy.deepcopy(locked.provenance or {})
    entity_provenance = dict(provenance.get("entity_registry") or {})
    entity_provenance["approved_for_downstream"] = False
    provenance["entity_registry"] = entity_provenance
    locked.payload = payload
    locked.provenance = provenance
    locked.status = StageOutput.Status.CHECKING
    locked.save(update_fields=["payload", "provenance", "status", "updated_at"])
    return proposal


def _approved_user_entity_rows(stage_output: StageOutput) -> list[dict[str, Any]]:
    proposals = NewIdProposal.objects.filter(
        document=stage_output.document,
        status=NewIdProposal.Status.APPROVED,
        payload__proposal_kind="entity_user_proposal",
    ).order_by("id")
    return [
        {
            "type": proposal.record_type,
            "id": proposal.proposed_id,
            "headword": proposal.headword,
            "form": proposal.evidence_form,
            "raw_line": (
                f"{proposal.record_type}: {proposal.headword} [{proposal.proposed_id}]"
                + (f" | Form: {proposal.evidence_form}" if proposal.evidence_form else "")
            ),
            "review_decision": "proposed_and_accepted",
            "review_provenance": {
                "proposal_id": proposal.pk,
                "proposal_contract": "entity-user-proposal-v1",
            },
        }
        for proposal in proposals
    ]


def entity_downstream_is_eligible(stage_output: StageOutput) -> bool:
    payload = stage_output.payload or {}
    review = payload.get("entity_review") or {}
    entity_provenance = (stage_output.provenance or {}).get("entity_registry") or {}
    return bool(
        stage_output.status == StageOutput.Status.ACCEPTED
        and review.get("contract_version") == REVIEW_CONTRACT
        and review.get("approved_for_downstream") is True
        and entity_provenance.get("approved_for_downstream") is True
        and isinstance(review.get("review_rows"), list)
        and isinstance(payload.get("reviewed_entity_registry"), list)
    )


def write_entity_downstream_package(*, stage_output: StageOutput, output_path: Path) -> dict[str, Any]:
    package = build_entity_downstream_package(stage_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    return package
