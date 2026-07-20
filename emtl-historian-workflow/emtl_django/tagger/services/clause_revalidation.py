from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tagger.models import StageOutput
from tagger.services.contracts import STAGE_CONTRACT_VERSION
from tagger.services.stage_runner import _split_structured_header_body
from tagger.services.stage_validation import (
    CLAUSE_OUTPUT_CONTRACT_VERSION,
    parse_clause_output_structure,
    validate_clause_coverage,
)


class ClauseRevalidationError(ValueError):
    """The stored StageOutput cannot be safely revalidated offline."""


@dataclass(frozen=True)
class ClauseRevalidationPlan:
    payload: dict[str, Any]
    provenance: dict[str, Any]
    report: dict[str, Any]


def build_clause_revalidation_plan(stage_output: StageOutput) -> ClauseRevalidationPlan:
    """Build a deterministic, side-effect-free revalidation update."""

    raw_output = str(stage_output.raw_output or "")
    payload_before = copy.deepcopy(dict(stage_output.payload or {}))
    provenance_before = copy.deepcopy(dict(stage_output.provenance or {}))
    request = dict(provenance_before.get("request") or {})

    _validate_preconditions(
        stage_output=stage_output,
        raw_output=raw_output,
        payload=payload_before,
        provenance=provenance_before,
        request=request,
    )

    metadata = dict(stage_output.document.metadata or {})
    working_source = str(metadata.get("working_source_text") or "").strip()
    structured_input = _split_structured_header_body(working_source)
    if structured_input is None:
        raise ClauseRevalidationError(
            "Document working_source_text does not contain a recoverable <END> Header boundary."
        )
    expected_header, expected_body = structured_input
    _validate_stored_request(
        stage_output=stage_output,
        request=request,
        provenance=provenance_before,
        working_source=working_source,
        expected_body=expected_body,
    )

    parsed = parse_clause_output_structure(raw_output)
    coverage = validate_clause_coverage(
        expected_body,
        parsed.clauses,
        expected_header=expected_header,
        generated_header=parsed.generated_header,
    )
    if not coverage.valid:
        raise ClauseRevalidationError(
            "Existing raw_output does not satisfy clause-parser-header-body-v1: "
            f"{coverage.message}"
        )

    payload_after = copy.deepcopy(payload_before)
    payload_after.update(
        {
            "output_contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
            "generated_header": parsed.generated_header,
            "header_was_bracket_wrapped": parsed.header_was_bracket_wrapped,
            "clauses": parsed.clauses,
            "coverage_validation": coverage.as_dict(),
            "notice": coverage.message,
        }
    )

    raw_sha256 = _text_sha256(raw_output)
    provenance_after = copy.deepcopy(provenance_before)
    provenance_after["offline_revalidation"] = {
        "command": "revalidate_clause_stage_output",
        "contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
        "stage_output_id": stage_output.pk,
        "document_id": stage_output.document.doc_id,
        "source": "existing_raw_output",
        "provider_called": False,
        "model_called": False,
        "regeneration_performed": False,
        "clauses_applied_to_document": False,
        "raw_output_sha256": raw_sha256,
        "structured_input_sha256": _text_sha256(working_source),
        "expected_header_sha256": _text_sha256(expected_header),
        "expected_body_sha256": _text_sha256(expected_body),
        "original_execution_finished_at": provenance_before.get("finished_at", ""),
    }

    payload_changed_paths = _changed_paths(payload_before, payload_after)
    provenance_changed_paths = _changed_paths(provenance_before, provenance_after)
    report = {
        "stage_output_id": stage_output.pk,
        "document_id": stage_output.document.doc_id,
        "stage_id": stage_output.stage,
        "lifecycle_status": stage_output.status,
        "execution_status": provenance_before.get("execution_status"),
        "output_contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
        "raw_output_sha256": raw_sha256,
        "parsed": {
            "generated_header": _text_summary(parsed.generated_header),
            "header_was_bracket_wrapped": parsed.header_was_bracket_wrapped,
            "clause_count": len(parsed.clauses),
            "clause_ids": [clause["clause_id"] for clause in parsed.clauses],
            "clauses_sha256": _json_sha256(parsed.clauses),
        },
        "validation": coverage.as_dict(),
        "payload_changed_paths": payload_changed_paths,
        "provenance_changed_paths": provenance_changed_paths,
        "payload_field_changes": _payload_field_changes(payload_before, payload_after),
        "semantic_change_required": bool(payload_changed_paths or provenance_changed_paths),
        "preservation": {
            "raw_output": True,
            "provider_payload": payload_before.get("provider_payload")
            == payload_after.get("provider_payload"),
            "provider_provenance": _without_key(provenance_before, "offline_revalidation")
            == _without_key(provenance_after, "offline_revalidation"),
            "lifecycle_status": True,
            "document_write_permitted": False,
            "clause_replacement_permitted": False,
        },
    }
    return ClauseRevalidationPlan(
        payload=payload_after,
        provenance=provenance_after,
        report=report,
    )


def _validate_preconditions(
    *,
    stage_output: StageOutput,
    raw_output: str,
    payload: dict[str, Any],
    provenance: dict[str, Any],
    request: dict[str, Any],
) -> None:
    if stage_output.stage != StageOutput.Stage.CLAUSE_PARSER:
        raise ClauseRevalidationError("StageOutput stage_id must be clause_parser.")
    if stage_output.status != StageOutput.Status.CHECKING:
        raise ClauseRevalidationError("StageOutput lifecycle status must be checking.")
    if provenance.get("execution_status") != "completed":
        raise ClauseRevalidationError("Stored execution_status must be completed.")
    if provenance.get("stage_id") != StageOutput.Stage.CLAUSE_PARSER:
        raise ClauseRevalidationError("Stored provenance stage_id must be clause_parser.")
    if request.get("stage_id") != StageOutput.Stage.CLAUSE_PARSER:
        raise ClauseRevalidationError("Stored request stage_id must be clause_parser.")
    if provenance.get("contract_version") != STAGE_CONTRACT_VERSION:
        raise ClauseRevalidationError("Stored provenance contract_version is unsupported.")
    if request.get("contract_version") != STAGE_CONTRACT_VERSION:
        raise ClauseRevalidationError("Stored request contract_version is unsupported.")
    if not raw_output.strip():
        raise ClauseRevalidationError("StageOutput raw_output is empty.")
    generated_output = payload.get("generated_output")
    if generated_output is not None and str(generated_output) != raw_output:
        raise ClauseRevalidationError("payload.generated_output differs from raw_output.")
    if list(provenance.get("errors") or []):
        raise ClauseRevalidationError("Stored execution contains provider errors.")


def _validate_stored_request(
    *,
    stage_output: StageOutput,
    request: dict[str, Any],
    provenance: dict[str, Any],
    working_source: str,
    expected_body: str,
) -> None:
    if request.get("document_id") != stage_output.document.doc_id:
        raise ClauseRevalidationError("Stored request document_id does not match the Document.")
    source_count = request.get("source_character_count")
    valid_source_counts = {len(working_source), len(expected_body)}
    if not isinstance(source_count, int) or source_count not in valid_source_counts:
        raise ClauseRevalidationError(
            "Stored request source_character_count does not match the structured input."
        )
    provider_inputs = dict((provenance.get("provider_api_payload") or {}).get("inputs") or {})
    stored_body_count = provider_inputs.get("document_body_character_count")
    if stored_body_count is not None and stored_body_count not in valid_source_counts:
        raise ClauseRevalidationError(
            "Stored provider input character count does not match the structured input."
        )


def _payload_field_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for key in (
        "output_contract_version",
        "generated_header",
        "header_was_bracket_wrapped",
        "clauses",
        "coverage_validation",
        "notice",
    ):
        if before.get(key) == after.get(key) and key in before:
            continue
        changed[key] = {
            "before": _report_value(key, before.get(key)),
            "after": _report_value(key, after.get(key)),
        }
    return changed


def _report_value(key: str, value: Any) -> Any:
    if key == "generated_header" and isinstance(value, str):
        return _text_summary(value)
    if key == "clauses" and isinstance(value, list):
        return {
            "count": len(value),
            "clause_ids": [item.get("clause_id") for item in value if isinstance(item, dict)],
            "sha256": _json_sha256(value),
        }
    return value


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(path)
            else:
                paths.extend(_changed_paths(before[key], after[key], path))
        return paths
    return [prefix or "$"]


def _without_key(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop(key, None)
    return result


def _text_summary(value: str) -> dict[str, Any]:
    return {"length": len(value), "sha256": _text_sha256(value)}


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _text_sha256(encoded)
