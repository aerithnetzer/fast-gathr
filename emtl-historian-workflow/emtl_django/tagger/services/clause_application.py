from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tagger.models import StageOutput
from tagger.services.stage_runner import _split_structured_header_body
from tagger.services.stage_validation import (
    CLAUSE_OUTPUT_CONTRACT_VERSION,
    normalize_coverage_text,
    parse_clause_output_structure,
    validate_clause_coverage,
)


APPLICATION_AUDIT_KEY = "clause_application"
APPLICATION_PROVENANCE_KEYS = {
    "clauses_applied_to_document",
    APPLICATION_AUDIT_KEY,
    "continued_at",
    "passed_forward_locally",
}


class ClauseApplicationError(ValueError):
    """A validated Clause Parser result is not safe to apply."""


@dataclass(frozen=True)
class ClauseApplicationPlan:
    clauses: list[dict[str, Any]]
    already_applied: bool
    clauses_already_materialized: bool
    report: dict[str, Any]


def build_clause_application_plan(stage_output: StageOutput) -> ClauseApplicationPlan:
    """Validate an application from frozen StageOutput data without writing."""

    payload = copy.deepcopy(dict(stage_output.payload or {}))
    provenance = copy.deepcopy(dict(stage_output.provenance or {}))
    raw_output = str(stage_output.raw_output or "")
    _validate_execution(stage_output, payload, provenance, raw_output)

    metadata = dict(stage_output.document.metadata or {})
    working_source = str(metadata.get("working_source_text") or "").strip()
    structured = _split_structured_header_body(working_source)
    if structured is None:
        raise ClauseApplicationError(
            "Document working_source_text has no deterministic <END> Header boundary."
        )
    expected_header, expected_body = structured

    clauses = _validated_payload_clauses(payload.get("clauses"))
    generated_header = str(payload.get("generated_header") or "")
    parsed = parse_clause_output_structure(raw_output)
    if parsed.generated_header != generated_header:
        raise ClauseApplicationError("raw_output Header differs from payload.generated_header.")
    if parsed.clauses != clauses:
        raise ClauseApplicationError("raw_output clauses differ from the frozen payload clauses.")
    if generated_header != expected_header:
        raise ClauseApplicationError("Generated Header does not exactly match the current Header.")

    coverage = validate_clause_coverage(
        expected_body,
        clauses,
        expected_header=expected_header,
        generated_header=generated_header,
    )
    stored_coverage = dict(payload.get("coverage_validation") or {})
    if not stored_coverage.get("valid"):
        raise ClauseApplicationError("Stored combined coverage is not valid.")
    if not dict(stored_coverage.get("header_validation") or {}).get("valid"):
        raise ClauseApplicationError("Stored Header validation is not valid.")
    if not dict(stored_coverage.get("body_validation") or {}).get("valid"):
        raise ClauseApplicationError("Stored body validation is not valid.")
    if not coverage.valid:
        raise ClauseApplicationError(
            f"Frozen clauses no longer cover the current structured input: {coverage.message}"
        )

    offline_audit = dict(provenance.get("offline_revalidation") or {})
    _validate_revalidation_baseline(
        stage_output=stage_output,
        offline_audit=offline_audit,
        raw_output=raw_output,
        working_source=working_source,
        expected_header=expected_header,
        expected_body=expected_body,
    )

    current_rows = _document_clause_rows(stage_output)
    planned_rows = _planned_clause_rows(clauses)
    planned_hash = json_sha256(planned_rows)
    applied_flag = provenance.get("clauses_applied_to_document") is True
    clauses_already_materialized = applied_flag and current_rows == planned_rows
    already_applied = applied_flag and stage_output.status == StageOutput.Status.ACCEPTED
    if already_applied:
        _validate_idempotent_state(
            stage_output=stage_output,
            provenance=provenance,
            current_rows=current_rows,
            planned_rows=planned_rows,
            planned_hash=planned_hash,
        )
    elif applied_flag:
        if stage_output.status != StageOutput.Status.CHECKING:
            raise ClauseApplicationError(
                "Pre-applied StageOutput lifecycle status must be checking."
            )
        if not clauses_already_materialized:
            raise ClauseApplicationError(
                "Pre-applied Document clauses differ from the frozen clauses."
            )
    elif stage_output.status != StageOutput.Status.CHECKING:
        raise ClauseApplicationError(
            "Unapplied StageOutput lifecycle status must be checking."
        )

    return ClauseApplicationPlan(
        clauses=clauses,
        already_applied=already_applied,
        clauses_already_materialized=clauses_already_materialized,
        report={
            "stage_output_id": stage_output.pk,
            "document_id": stage_output.document.doc_id,
            "stage_id": stage_output.stage,
            "output_contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
            "idempotent_noop": already_applied,
            "clauses_already_materialized": clauses_already_materialized,
            "before": {
                "lifecycle_status": stage_output.status,
                "clause_count": len(current_rows),
                "clause_ids": [row["clause_id"] for row in current_rows],
                "clauses_sha256": json_sha256(current_rows),
            },
            "after": {
                "lifecycle_status": StageOutput.Status.ACCEPTED,
                "clause_count": len(planned_rows),
                "clause_ids": [row["clause_id"] for row in planned_rows],
                "clauses_sha256": planned_hash,
            },
            "coverage": coverage.as_dict(),
            "header": {
                "exact_match": generated_header == expected_header,
                "sha256": text_sha256(expected_header),
                "normalized_length": len(normalize_coverage_text(expected_header)),
                "stored_validation_valid": True,
            },
            "body": {
                "sha256": text_sha256(expected_body),
                "normalized_length": len(normalize_coverage_text(expected_body)),
                "stored_validation_valid": True,
            },
            "baselines": {
                "raw_output_sha256": text_sha256(raw_output),
                "working_source_text_sha256": text_sha256(working_source),
            },
            "planned_provenance_changes": (
                []
                if already_applied
                else [
                    "clauses_applied_to_document",
                    APPLICATION_AUDIT_KEY,
                    "continued_at",
                    "passed_forward_locally",
                ]
            ),
            "preservation": {
                "generated_header": True,
                "document_metadata": True,
                "working_source_text": True,
                "raw_output": True,
                "payload": True,
                "provider_evidence": True,
                "generation_provenance": True,
                "other_documents": True,
                "other_stage_outputs": True,
                "provider_called": False,
                "model_called": False,
            },
        },
    )


def generation_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in provenance.items()
        if key not in APPLICATION_PROVENANCE_KEYS
    }


def planned_clause_rows(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _planned_clause_rows(clauses)


def text_sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return text_sha256(encoded)


def _validate_execution(
    stage_output: StageOutput,
    payload: dict[str, Any],
    provenance: dict[str, Any],
    raw_output: str,
) -> None:
    if stage_output.stage != StageOutput.Stage.CLAUSE_PARSER:
        raise ClauseApplicationError("StageOutput stage_id must be clause_parser.")
    if provenance.get("execution_status") != "completed":
        raise ClauseApplicationError("Stored execution_status must be completed.")
    if provenance.get("real_chatbot_execution") is not True:
        raise ClauseApplicationError("real_chatbot_execution must be true.")
    if payload.get("output_contract_version") != CLAUSE_OUTPUT_CONTRACT_VERSION:
        raise ClauseApplicationError("Unsupported or missing Clause Parser output contract.")
    if not raw_output.strip():
        raise ClauseApplicationError("StageOutput raw_output is empty.")
    if payload.get("generated_output") is not None and payload.get("generated_output") != raw_output:
        raise ClauseApplicationError("payload.generated_output differs from raw_output.")


def _validated_payload_clauses(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ClauseApplicationError("Frozen payload must contain at least one clause.")
    clauses = copy.deepcopy(value)
    if any(not isinstance(clause, dict) for clause in clauses):
        raise ClauseApplicationError("Every frozen clause must be an object.")
    clause_count = len(clauses)
    expected_ids = [f"{index:03d}" for index in range(1, clause_count + 1)]
    actual_ids = [str(clause.get("clause_id") or "") for clause in clauses]
    if actual_ids != expected_ids or len(set(actual_ids)) != clause_count:
        raise ClauseApplicationError(
            f"Clause IDs must be unique and consecutive from 001 to {clause_count:03d}."
        )
    actual_sequences = [clause.get("sequence") for clause in clauses]
    if actual_sequences != list(range(1, clause_count + 1)):
        raise ClauseApplicationError(
            f"Clause sequence values must be consecutive from 1 to {clause_count}."
        )
    if any(not str(clause.get("text") or "") for clause in clauses):
        raise ClauseApplicationError("Every frozen clause must contain non-empty text.")
    return clauses


def _validate_revalidation_baseline(
    *,
    stage_output: StageOutput,
    offline_audit: dict[str, Any],
    raw_output: str,
    working_source: str,
    expected_header: str,
    expected_body: str,
) -> None:
    if offline_audit.get("stage_output_id") != stage_output.pk:
        raise ClauseApplicationError("Offline revalidation StageOutput baseline is missing or mismatched.")
    if offline_audit.get("contract_version") != CLAUSE_OUTPUT_CONTRACT_VERSION:
        raise ClauseApplicationError("Offline revalidation contract baseline is mismatched.")
    expected_hashes = {
        "raw_output_sha256": text_sha256(raw_output),
        "structured_input_sha256": text_sha256(working_source),
        "expected_header_sha256": text_sha256(expected_header),
        "expected_body_sha256": text_sha256(expected_body),
    }
    for key, value in expected_hashes.items():
        if offline_audit.get(key) != value:
            raise ClauseApplicationError(f"Offline revalidation baseline mismatch: {key}.")
    if offline_audit.get("provider_called") is not False:
        raise ClauseApplicationError("Offline revalidation provider audit is not false.")
    if offline_audit.get("model_called") is not False:
        raise ClauseApplicationError("Offline revalidation model audit is not false.")


def _validate_idempotent_state(
    *,
    stage_output: StageOutput,
    provenance: dict[str, Any],
    current_rows: list[dict[str, Any]],
    planned_rows: list[dict[str, Any]],
    planned_hash: str,
) -> None:
    if stage_output.status != StageOutput.Status.ACCEPTED:
        raise ClauseApplicationError("Applied StageOutput lifecycle status must be accepted.")
    if current_rows != planned_rows:
        raise ClauseApplicationError("Applied Document clauses differ from the frozen clauses.")
    audit = dict(provenance.get(APPLICATION_AUDIT_KEY) or {})
    if audit.get("stage_output_id") != stage_output.pk:
        raise ClauseApplicationError("Clause application audit StageOutput ID is mismatched.")
    if audit.get("contract_version") != CLAUSE_OUTPUT_CONTRACT_VERSION:
        raise ClauseApplicationError("Clause application audit contract is mismatched.")
    if audit.get("after_clauses_sha256") != planned_hash:
        raise ClauseApplicationError("Clause application audit hash is mismatched.")


def _document_clause_rows(stage_output: StageOutput) -> list[dict[str, Any]]:
    return [
        {
            "clause_id": clause.clause_id,
            "sequence": clause.sequence,
            "text": clause.text,
        }
        for clause in stage_output.document.clauses.order_by("sequence", "pk")
    ]


def _planned_clause_rows(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "clause_id": str(clause.get("clause_id") or index).zfill(3),
            "sequence": index,
            "text": str(clause.get("text") or ""),
        }
        for index, clause in enumerate(clauses, start=1)
    ]
