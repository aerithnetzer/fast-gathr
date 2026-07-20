from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


CLAUSE_MARKER = re.compile(r"(?im)^[ \t]*CLAUSE[ \t]+(\d+)[ \t]*$")
CLAUSE_OUTPUT_CONTRACT_VERSION = "clause-parser-header-body-v1"


@dataclass(frozen=True)
class ParsedClauseOutput:
    generated_header: str
    clauses: list[dict[str, Any]]
    header_was_bracket_wrapped: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
            "generated_header": self.generated_header,
            "header_was_bracket_wrapped": self.header_was_bracket_wrapped,
            "clauses": self.clauses,
        }


@dataclass(frozen=True)
class ClauseCoverageResult:
    valid: bool
    source_normalized_length: int
    output_normalized_length: int
    clause_count: int
    first_mismatch_index: int | None
    message: str
    header_required: bool
    header_valid: bool
    expected_header_normalized_length: int
    generated_header_normalized_length: int
    header_first_mismatch_index: int | None
    body_valid: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "normalization": "collapse all whitespace runs to one ASCII space; trim ends",
            "source_normalized_length": self.source_normalized_length,
            "output_normalized_length": self.output_normalized_length,
            "clause_count": self.clause_count,
            "first_mismatch_index": self.first_mismatch_index,
            "message": self.message,
            "contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
            "header_validation": {
                "required": self.header_required,
                "valid": self.header_valid,
                "normalization": "collapse all whitespace runs to one ASCII space; trim ends",
                "expected_normalized_length": self.expected_header_normalized_length,
                "generated_normalized_length": self.generated_header_normalized_length,
                "first_mismatch_index": self.header_first_mismatch_index,
            },
            "body_validation": {
                "valid": self.body_valid,
                "normalization": "collapse all whitespace runs to one ASCII space; trim ends",
                "source_normalized_length": self.source_normalized_length,
                "output_normalized_length": self.output_normalized_length,
                "clause_count": self.clause_count,
                "first_mismatch_index": self.first_mismatch_index,
            },
        }


def parse_clause_output_structure(raw_output: str) -> ParsedClauseOutput:
    raw_text = str(raw_output or "")
    matches = list(CLAUSE_MARKER.finditer(raw_text))
    header_text = raw_text[: matches[0].start()].strip() if matches else raw_text.strip()
    header_was_bracket_wrapped = (
        len(header_text) >= 2 and header_text.startswith("[") and header_text.endswith("]")
    )
    if header_was_bracket_wrapped:
        header_text = header_text[1:-1].strip()
    clauses: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_text)
        text = raw_text[start:end].strip()
        clauses.append(
            {
                "clause_id": str(match.group(1)).zfill(3),
                "sequence": index + 1,
                "text": text,
            }
        )
    return ParsedClauseOutput(
        generated_header=header_text,
        clauses=clauses,
        header_was_bracket_wrapped=header_was_bracket_wrapped,
    )


def parse_clause_output(raw_output: str) -> list[dict[str, Any]]:
    """Backward-compatible clause-only view of the structured parser result."""

    return parse_clause_output_structure(raw_output).clauses


def validate_clause_coverage(
    source_body: str,
    clauses: list[dict[str, Any]],
    *,
    expected_header: str | None = None,
    generated_header: str = "",
) -> ClauseCoverageResult:
    header_required = expected_header is not None
    expected_header_normalized = normalize_coverage_text(expected_header or "")
    generated_header_normalized = normalize_coverage_text(generated_header)
    header_mismatch = (
        _first_mismatch(expected_header_normalized, generated_header_normalized)
        if header_required
        else None
    )
    header_valid = (
        not header_required or expected_header_normalized == generated_header_normalized
    )
    source = normalize_coverage_text(source_body)
    output = normalize_coverage_text(" ".join(str(clause.get("text") or "") for clause in clauses))
    mismatch = _first_mismatch(source, output)
    body_valid = bool(clauses) and source == output
    valid = header_valid and body_valid
    if not clauses:
        message = "No CLAUSE nnn blocks were parsed from the model output."
    elif not header_valid:
        message = "Generated Header does not reproduce the expected Header after whitespace normalization."
    elif body_valid:
        message = (
            "Generated Header and body clauses cover the structured input exactly once "
            "after whitespace normalization."
            if header_required
            else "Generated clauses cover the source body exactly once after whitespace normalization."
        )
    else:
        message = "Generated clauses do not reproduce the source body exactly after whitespace normalization."
    return ClauseCoverageResult(
        valid=valid,
        source_normalized_length=len(source),
        output_normalized_length=len(output),
        clause_count=len(clauses),
        first_mismatch_index=mismatch,
        message=message,
        header_required=header_required,
        header_valid=header_valid,
        expected_header_normalized_length=len(expected_header_normalized),
        generated_header_normalized_length=len(generated_header_normalized),
        header_first_mismatch_index=header_mismatch,
        body_valid=body_valid,
    )


def normalize_coverage_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first_mismatch(left: str, right: str) -> int | None:
    for index, (left_char, right_char) in enumerate(zip(left, right)):
        if left_char != right_char:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def validate_required_outputs(
    required_stage_ids: tuple[str, ...],
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    missing = []
    for stage_id in required_stage_ids:
        output = stage_outputs.get(stage_id)
        if output is None or not str(getattr(output, "raw_output", "") or "").strip():
            missing.append(stage_id)
    return {
        "valid": not missing,
        "required_stage_ids": list(required_stage_ids),
        "missing_stage_ids": missing,
    }
