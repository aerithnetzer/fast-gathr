from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from django.db import transaction

from tagger.models import StageExecutionAttempt, StageOutput

from .contracts import ExecutionStatus


@dataclass(frozen=True)
class EntityAttemptPersistenceOutcome:
    attempt_id: int
    stage_output_id: int | None
    disposition: str
    applied_to_stage_output: bool
    accepted_stage_output_protected: bool
    validation_valid: bool
    requires_human_review: bool
    source_complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "stage_output_id": self.stage_output_id,
            "disposition": self.disposition,
            "applied_to_stage_output": self.applied_to_stage_output,
            "accepted_stage_output_protected": self.accepted_stage_output_protected,
            "validation_valid": self.validation_valid,
            "requires_human_review": self.requires_human_review,
            "source_complete": self.source_complete,
        }


class DuplicateEntityRequestError(RuntimeError):
    pass


@transaction.atomic
def reserve_entity_registry_attempt(
    *,
    stage_output: StageOutput,
    request_id: str,
    provenance: dict[str, Any],
) -> StageExecutionAttempt:
    if stage_output.stage != StageOutput.Stage.ENTITY_REGISTRY:
        raise ValueError("Entity attempt reservation requires an Entity Registry StageOutput.")
    cleaned_request_id = str(request_id or "").strip()
    if not cleaned_request_id:
        raise ValueError("Entity attempt reservation requires an explicit request ID.")
    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    if StageExecutionAttempt.objects.filter(request_id=cleaned_request_id).exists():
        raise DuplicateEntityRequestError(
            f"Entity request ID has already been reserved: {cleaned_request_id}"
        )
    return StageExecutionAttempt.objects.create(
        stage_output=locked,
        request_id=cleaned_request_id,
        stage=locked.stage,
        execution_status="preflight_pending",
        disposition=StageExecutionAttempt.Disposition.RECORDED_ONLY,
        provider="gpu_local",
        model="Qwen2.5-32B-Instruct",
        provenance=dict(provenance),
        validation={"valid": False, "source_complete": False},
        applied_to_stage_output=False,
    )


@transaction.atomic
def persist_entity_registry_attempt(
    *,
    stage_output: StageOutput,
    result: Any,
    reserved_attempt: StageExecutionAttempt | None = None,
) -> EntityAttemptPersistenceOutcome:
    """Record every real attempt, but apply only a valid, source-complete result.

    Attempts are stored separately so an invalid result never mutates the current
    StageOutput or its Document. An already accepted StageOutput is immutable here.
    """

    if stage_output.stage != StageOutput.Stage.ENTITY_REGISTRY:
        raise ValueError("Entity attempt persistence requires an Entity Registry StageOutput.")

    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    payload = dict(getattr(result, "payload", {}) or {})
    provenance = dict(getattr(result, "provenance", {}) or {})
    validation = _entity_validation_payload(result, payload)
    validation_valid = bool(validation.get("valid"))
    requires_human_review = bool(validation.get("requires_human_review"))
    source_complete = _source_package_is_complete(provenance)
    result_status = str(getattr(result, "status", "") or "")
    completed_and_valid = (
        result_status == ExecutionStatus.COMPLETED.value
        and validation_valid
        and source_complete
    )
    parsed_tags = _parsed_entity_tags(payload)
    real_review_candidate = bool(
        source_complete
        and result_status in {ExecutionStatus.COMPLETED.value, ExecutionStatus.VALIDATION_FAILED.value}
        and provenance.get("real_chatbot_execution") is True
        and provenance.get("model_call_completed") is True
        and str(getattr(result, "raw_output", "") or "").strip()
        and parsed_tags
        and not str(getattr(result, "error", "") or "").strip()
    )

    if locked.status == StageOutput.Status.ACCEPTED:
        disposition = StageExecutionAttempt.Disposition.ACCEPTED_PROTECTED
        apply_result = False
        accepted_protected = True
    elif completed_and_valid or real_review_candidate:
        disposition = StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING
        apply_result = True
        accepted_protected = False
    else:
        disposition = StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED
        apply_result = False
        accepted_protected = False

    if reserved_attempt is None:
        request_payload = getattr(result, "request", {}) or {}
        request_id = str(request_payload.get("request_id") or "").strip() or None
        attempt = StageExecutionAttempt(stage_output=locked, request_id=request_id)
    else:
        attempt = StageExecutionAttempt.objects.select_for_update().get(
            pk=reserved_attempt.pk
        )
        if attempt.stage_output_id != locked.pk:
            raise ValueError("Reserved Entity attempt belongs to another StageOutput.")
    provenance = {**dict(attempt.provenance or {}), **provenance}
    attempt.stage = locked.stage
    attempt.execution_status = result_status
    attempt.disposition = disposition
    attempt.provider = str(
        getattr(result, "provider", "") or provenance.get("provider") or ""
    )
    attempt.model = str(getattr(result, "model", "") or provenance.get("model") or "")
    attempt.raw_output = str(getattr(result, "raw_output", "") or "")
    attempt.payload = payload
    attempt.provenance = provenance
    attempt.validation = {**validation, "source_complete": source_complete}
    attempt.error = str(getattr(result, "error", "") or "")
    attempt.applied_to_stage_output = apply_result
    attempt.save()

    if apply_result:
        payload["entity_validation"] = validation
        payload["entity_registry"] = parsed_tags
        payload["entity_review"] = {
            "state": "review_candidate",
            "attempt_id": attempt.pk,
            "deterministic_validation_valid": validation_valid,
            "requires_human_review": requires_human_review,
            "human_review_reasons": [
                issue for issue in validation.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "human_review"
            ],
            "approved_for_downstream": False,
        }
        payload["runner"] = {
            "status": result_status,
            "provider": attempt.provider,
            "model": attempt.model,
            "message": "Entity Registry output recorded as a visible review candidate.",
            "attempt_id": attempt.pk,
            "requires_human_review": requires_human_review,
        }
        provenance["entity_registry"] = {
            "attempt_id": attempt.pk,
            "registry_version": validation.get("registry_version", ""),
            "resource_hashes": validation.get("resource_hashes", {}),
            "validation_valid": validation_valid,
            "requires_human_review": requires_human_review,
            "source_complete": True,
            "review_candidate": True,
            "approved_for_downstream": False,
        }
        locked.payload = payload
        locked.raw_output = attempt.raw_output
        locked.provenance = provenance
        locked.status = StageOutput.Status.CHECKING
        locked.save(
            update_fields=["payload", "raw_output", "provenance", "status", "updated_at"]
        )

    return EntityAttemptPersistenceOutcome(
        attempt_id=int(attempt.pk),
        stage_output_id=int(locked.pk) if apply_result else None,
        disposition=disposition,
        applied_to_stage_output=apply_result,
        accepted_stage_output_protected=accepted_protected,
        validation_valid=validation_valid,
        requires_human_review=requires_human_review,
        source_complete=source_complete,
    )


@transaction.atomic
def promote_entity_attempt_to_review_candidate(*, attempt_id: int) -> EntityAttemptPersistenceOutcome:
    """Promote an eligible stored real attempt without another model call."""
    attempt = StageExecutionAttempt.objects.select_for_update().select_related("stage_output").get(pk=attempt_id)
    result = SimpleNamespace(
        status=attempt.execution_status,
        payload=attempt.payload,
        provenance=attempt.provenance,
        validation={"entity_output": attempt.validation},
        provider=attempt.provider,
        model=attempt.model,
        raw_output=attempt.raw_output,
        error=attempt.error,
        request={"request_id": attempt.request_id},
    )
    outcome = persist_entity_registry_attempt(
        stage_output=attempt.stage_output,
        result=result,
        reserved_attempt=attempt,
    )
    if not outcome.applied_to_stage_output and not outcome.accepted_stage_output_protected:
        raise ValueError("Attempt is not eligible for Entity review-candidate promotion.")
    return outcome


def _parsed_entity_tags(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entity_output = payload.get("entity_output")
    if not isinstance(entity_output, dict):
        return []
    tags = entity_output.get("tags")
    if not isinstance(tags, list):
        return []
    return [dict(tag) for tag in tags if isinstance(tag, dict)]


def _entity_validation_payload(result: Any, payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("entity_validation")
    if isinstance(candidate, dict):
        return dict(candidate)
    result_validation = getattr(result, "validation", {}) or {}
    if isinstance(result_validation, dict):
        candidate = result_validation.get("entity_output")
        if isinstance(candidate, dict):
            return dict(candidate)
    return {
        "valid": False,
        "issues": [
            {
                "code": "entity_validation_missing",
                "message": "No structured Entity Registry validation result was supplied.",
                "severity": "error",
            }
        ],
    }


def _source_package_is_complete(provenance: dict[str, Any]) -> bool:
    bundle = provenance.get("bundle")
    if not isinstance(bundle, dict):
        return False
    loaded_files = bundle.get("loaded_files")
    if not isinstance(loaded_files, list):
        return False
    for item in loaded_files:
        if not isinstance(item, dict):
            return False
        loaded = int(item.get("characters_loaded") or 0)
        available = int(item.get("characters_available") or 0)
        if bool(item.get("truncated")) or loaded != available:
            return False
    candidate_package = bundle.get("entity_candidate_package")
    indexed_resources = bundle.get("indexed_resources")
    if isinstance(candidate_package, dict) and candidate_package:
        return bool(
            len(loaded_files) == 3
            and isinstance(indexed_resources, list)
            and len(indexed_resources) == 3
            and all(
                isinstance(item, dict)
                and item.get("fully_indexed")
                and item.get("source_hash")
                for item in indexed_resources
            )
            and candidate_package.get("source_complete")
            and candidate_package.get("provenance_complete")
            and candidate_package.get("selection_policy_complete")
            and candidate_package.get("mandatory_candidates_retained")
            and candidate_package.get("candidate_package_complete")
        )
    return len(loaded_files) == 6
