from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tagger.models import StageOutput

from .contracts import ExecutionStatus
from .entity_review_handoff import (
    build_entity_downstream_package,
    entity_downstream_is_eligible,
)
from .event_occurrence_workflow import (
    build_merged_event_occurrence_package,
    load_accepted_event_assignments,
)
from .providers.factory import StageGenerationClient, stage_generation_client


CONTRACT_VERSION = "occurrence-generation-gpu-v1"
OUTPUT_CONTRACT_VERSION = "occurrence-clause-eaq-v1"
RESOURCE_DIR = Path(__file__).resolve().parents[3] / "Chatbot docs" / "Claude chatbots"
RESOURCE_PATHS = {
    "system_prompt": RESOURCE_DIR / "Occurrences_Registry_System_Prompt.txt",
    "instructions": RESOURCE_DIR / "Occurrences_Registry_Instructions.txt",
    "legal_boilerplate": RESOURCE_DIR / "Legal_Boilerplate.txt",
    "events": RESOURCE_DIR / "Events_List.csv",
    "social_identities": RESOURCE_DIR / "Social_Identity_List.csv",
    "quantified_statements": RESOURCE_DIR / "Quantified_Statements_List.csv",
    "attributes": RESOURCE_DIR / "Attributes_List.csv",
}

CLAUSE_BLOCK = re.compile(
    r"(?ims)^\s*CLAUSE\s+(\d+)\s*$\n(.*?)(?=^\s*CLAUSE\s+\d+\s*$|\Z)"
)
TAG_LINE = re.compile(
    r"^(?P<type>[EAQ]):\s*(?P<headword>.+?)\s*\[(?P<id>(?:NEW-)?[EAQ]-\d+)\]\s*(?P<fields>\|.*)?$"
)
BRACKET_ID = re.compile(r"\[((?:NEW-)?[A-Z]+-\d+)\]")
TOKEN = re.compile(r"[a-z0-9]+")


class OccurrenceGenerationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OccurrenceRunResult:
    status: str
    raw_output: str
    payload: dict[str, Any]
    provenance: dict[str, Any]
    validation: dict[str, Any]
    provider: str = "gpu_local"
    model: str = ""
    error: str = ""
    request: dict[str, Any] | None = None


class OccurrenceGenerationService:
    def __init__(self, *, provider_client: StageGenerationClient | None = None) -> None:
        self.provider_client = provider_client or stage_generation_client()

    def run(
        self,
        *,
        clause_output: StageOutput,
        entity_output: StageOutput,
        eventcut_output: StageOutput,
        clause_ids: list[str],
        event_review_store_path: Path,
        request_id: str,
        max_output_tokens: int = 2048,
    ) -> OccurrenceRunResult:
        clauses = _accepted_clause_records(clause_output, clause_ids)
        entity_package = _approved_entity_package(clause_output, entity_output)
        if eventcut_output.document_id != clause_output.document_id:
            raise OccurrenceGenerationError(
                "eventcut_document_mismatch", "EventCut and Clause outputs belong to different documents"
            )
        assignment_package = load_accepted_event_assignments(
            review_store_path=event_review_store_path,
            eventcut_output=eventcut_output,
            clause_ids=[row["clause_id"] for row in clauses],
        )
        resources = _load_resources()
        authority = _bounded_authority(
            clauses=clauses,
            entity_package=entity_package,
            event_assignments=assignment_package["assignments"],
            resources=resources,
        )
        system_prompt, user_prompt = _build_prompt(
            resources=resources,
            clauses=clauses,
            entity_package=entity_package,
            assignment_package=assignment_package,
            authority=authority,
        )
        request_payload = _provider_payload(
            request_id=request_id,
            document_id=clause_output.document.doc_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            clauses=clauses,
            entity_package=entity_package,
            max_output_tokens=max_output_tokens,
        )
        response = self.provider_client.generate(request_payload)
        base_provenance = {
            "contract_version": CONTRACT_VERSION,
            "provider": response.provider,
            "model": response.model,
            "real_chatbot_execution": response.real_chatbot_execution,
            "request_id": request_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_clause_stage_output_id": clause_output.pk,
            "source_entity_stage_output_id": entity_output.pk,
            "source_eventcut_stage_output_id": eventcut_output.pk,
            "selected_clause_ids": [row["clause_id"] for row in clauses],
            "entity_review_contract": entity_package["contract_version"],
            "entity_reviewed_row_count": len(entity_package["reviewed_rows"]),
            "accepted_event_assignment_count": assignment_package["assignment_count"],
            "resource_audit": _resource_audit(resources),
            "authority_selection": authority["provenance"],
            "prompt_character_count": len(system_prompt) + len(user_prompt),
            "provider_metadata": response.metadata,
        }
        if response.status != ExecutionStatus.COMPLETED.value:
            return OccurrenceRunResult(
                status=response.status,
                raw_output=response.raw_output,
                payload={},
                provenance=base_provenance,
                validation={"valid": False, "model_call_completed": False},
                provider=response.provider,
                model=response.model,
                error=response.error,
                request=request_payload,
            )
        validation = validate_occurrence_enrichment_output(
            raw_output=response.raw_output,
            clauses=clauses,
            approved_entity_rows=entity_package["reviewed_rows"],
            allowed_authority_ids=authority["allowed_ids"],
            authority_headwords=authority["authority_headwords"],
            approved_event_assignments=assignment_package["assignments"],
        )
        payload = {
            "contract_version": OUTPUT_CONTRACT_VERSION,
            "doc_id": clause_output.document.doc_id,
            "source_clause_stage_output_id": clause_output.pk,
            "source_entity_stage_output_id": entity_output.pk,
            "source_eventcut_stage_output_id": eventcut_output.pk,
            "selected_clause_ids": [row["clause_id"] for row in clauses],
            "event_assignment_package": assignment_package,
            "parsed_clauses": validation["parsed_clauses"],
            "validation": validation,
            "review_state": "review_candidate",
            "approved_for_downstream": False,
        }
        payload["merged_review_package"] = build_merged_event_occurrence_package(
            document_id=clause_output.document.doc_id,
            clauses=clauses,
            assignment_package=assignment_package,
            occurrence_payload=payload,
        )
        return OccurrenceRunResult(
            status=(
                ExecutionStatus.COMPLETED.value
                if validation["valid"]
                else ExecutionStatus.VALIDATION_FAILED.value
            ),
            raw_output=response.raw_output,
            payload=payload,
            provenance={
                **base_provenance,
                "model_call_completed": True,
                "output_validation_valid": validation["valid"],
            },
            validation=validation,
            provider=response.provider,
            model=response.model,
            error="" if validation["valid"] else "Occurrence output failed validation.",
            request=request_payload,
        )


def validate_edited_occurrence_output(
    *, raw_output: str, clause_output: StageOutput,
    entity_output: StageOutput, occurrence_output: StageOutput,
) -> dict[str, Any]:
    payload = occurrence_output.payload or {}
    clause_ids = list(payload.get("selected_clause_ids") or [])
    clauses = _accepted_clause_records(clause_output, clause_ids)
    entity_package = _approved_entity_package(clause_output, entity_output)
    assignment_package = payload.get("event_assignment_package") or {}
    assignments = list(assignment_package.get("assignments") or [])
    authority = _bounded_authority(
        clauses=clauses,
        entity_package=entity_package,
        event_assignments=assignments,
        resources=_load_resources(),
    )
    validation = validate_occurrence_enrichment_output(
        raw_output=raw_output,
        clauses=clauses,
        approved_entity_rows=entity_package["reviewed_rows"],
        allowed_authority_ids=authority["allowed_ids"],
        authority_headwords=authority["authority_headwords"],
        approved_event_assignments=assignments,
    )
    validation["source_clauses"] = clauses
    return validation


def validate_occurrence_output(
    *,
    raw_output: str,
    clauses: list[dict[str, Any]],
    approved_entity_rows: list[dict[str, Any]],
    allowed_authority_ids: set[str],
    authority_headwords: dict[str, str] | None = None,
) -> dict[str, Any]:
    clause_by_id = {row["clause_id"]: row for row in clauses}
    approved_entity_ids = {
        str(row.get("id") or "") for row in approved_entity_rows if row.get("id")
    }
    parsed_clauses = []
    issues = []
    seen_clause_ids = []
    tag_count = 0
    for match in CLAUSE_BLOCK.finditer(str(raw_output or "")):
        clause_id = str(match.group(1)).zfill(3)
        seen_clause_ids.append(clause_id)
        source_clause = clause_by_id.get(clause_id)
        if source_clause is None:
            issues.append(_issue("unexpected_clause", clause_id, "Output contains an unrequested clause"))
            continue
        block_lines = [line.strip() for line in match.group(2).splitlines() if line.strip()]
        tags = []
        for line in block_lines:
            tag_match = TAG_LINE.match(line)
            if not tag_match:
                continue
            fields = _parse_fields(tag_match.group("fields") or "")
            tag = {
                "type": tag_match.group("type"),
                "headword": tag_match.group("headword").strip(),
                "id": tag_match.group("id"),
                "fields": fields,
                "raw_line": line,
            }
            tags.append(tag)
            tag_count += 1
            trigger = str(fields.get("Trigger") or "").strip()
            if not trigger:
                issues.append(_issue("trigger_required", clause_id, "Every E/A/Q tag requires Trigger", line))
            elif _normalized(trigger) not in _normalized(source_clause["text"]):
                issues.append(_issue("trigger_not_in_clause", clause_id, f"Trigger is not in Clause: {trigger}", line))
            if not tag["id"].startswith("NEW-") and tag["id"] not in allowed_authority_ids:
                issues.append(_issue("authority_id_not_supplied", clause_id, f"ID was not in bounded authority: {tag['id']}", line))
            official_headword = (authority_headwords or {}).get(tag["id"])
            if official_headword and _normalized(official_headword) != _normalized(tag["headword"]):
                issues.append(
                    _issue(
                        "authority_headword_mismatch",
                        clause_id,
                        f"{tag['id']} is {official_headword}, not {tag['headword']}",
                        line,
                    )
                )
            for key, value in fields.items():
                if key in {"Actor", "Counterparty", "Object", "Beneficiary", "Location", "Instrument"}:
                    for reference in BRACKET_ID.findall(value):
                        if reference.startswith("NEW-") or reference.startswith("SI-"):
                            continue
                        if reference not in approved_entity_ids:
                            issues.append(_issue("unapproved_entity_reference", clause_id, f"Referenced Entity was not approved: {reference}", line))
        parsed_clauses.append(
            {
                "clause_id": clause_id,
                "source_text": source_clause["text"],
                "tags": tags,
            }
        )
    expected = [row["clause_id"] for row in clauses]
    if seen_clause_ids != expected:
        issues.append(
            {
                "code": "clause_sequence_mismatch",
                "message": f"Expected clauses {expected}, received {seen_clause_ids}",
            }
        )
    if not tag_count:
        issues.append({"code": "no_occurrence_tags", "message": "No E/A/Q tags were parsed"})
    return {
        "valid": not issues,
        "contract_version": OUTPUT_CONTRACT_VERSION,
        "selected_clause_ids": expected,
        "parsed_clause_count": len(parsed_clauses),
        "tag_count": tag_count,
        "issues": issues,
        "parsed_clauses": parsed_clauses,
        "approved_entity_id_count": len(approved_entity_ids),
        "allowed_authority_id_count": len(allowed_authority_ids),
    }


def validate_occurrence_enrichment_output(
    *,
    raw_output: str,
    clauses: list[dict[str, Any]],
    approved_entity_rows: list[dict[str, Any]],
    allowed_authority_ids: set[str],
    authority_headwords: dict[str, str],
    approved_event_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    result = validate_occurrence_output(
        raw_output=raw_output,
        clauses=clauses,
        approved_entity_rows=approved_entity_rows,
        allowed_authority_ids=allowed_authority_ids,
        authority_headwords=authority_headwords,
    )
    assignments_by_clause: dict[str, list[dict[str, Any]]] = {}
    for assignment in approved_event_assignments:
        assignments_by_clause.setdefault(_normalized_clause_id(assignment.get("clause_id")), []).append(
            assignment
        )
    warnings = []
    enrichment_issues = []
    for clause in result["parsed_clauses"]:
        clause_id = _normalized_clause_id(clause.get("clause_id"))
        remaining = list(assignments_by_clause.get(clause_id, []))
        accepted_tags = []
        unresolved = []
        for tag in clause.get("tags") or []:
            if tag.get("type") != "E":
                accepted_tags.append(tag)
                continue
            match = next(
                (
                    assignment
                    for assignment in remaining
                    if assignment.get("event_id") == tag.get("id")
                    and _normalized(assignment.get("headword")) == _normalized(tag.get("headword"))
                ),
                None,
            )
            if match is None:
                unresolved.append(
                    {
                        "status": "unresolved_event_suggestion",
                        "reason": "Occurrence model proposed an Event not present in human-approved EventCut assignments",
                        "event_id": tag.get("id"),
                        "headword": tag.get("headword"),
                        "fields": tag.get("fields"),
                        "raw_line": tag.get("raw_line"),
                        "return_to_stage": "eventcut_headword_review",
                    }
                )
                warnings.append(
                    {
                        "code": "unresolved_event_suggestion",
                        "clause_id": clause_id,
                        "event_id": tag.get("id"),
                        "headword": tag.get("headword"),
                    }
                )
                continue
            enriched = {**tag, "event_cut_id": match["event_cut_id"], "assignment_id": match["assignment_id"]}
            accepted_tags.append(enriched)
            remaining.remove(match)
        for assignment in remaining:
            enrichment_issues.append(
                {
                    "code": "accepted_event_missing_occurrence",
                    "clause_id": clause_id,
                    "event_cut_id": assignment["event_cut_id"],
                    "event_id": assignment["event_id"],
                    "headword": assignment["headword"],
                    "message": "Occurrence output omitted a human-approved Event assignment",
                }
            )
        clause["tags"] = accepted_tags
        clause["unresolved_event_suggestions"] = unresolved
    result["issues"] = list(result["issues"]) + enrichment_issues
    result["warnings"] = warnings
    result["unresolved_event_suggestion_count"] = len(warnings)
    result["accepted_event_assignment_count"] = len(approved_event_assignments)
    result["accepted_event_occurrence_count"] = sum(
        1
        for clause in result["parsed_clauses"]
        for tag in clause.get("tags") or []
        if tag.get("type") == "E" and tag.get("event_cut_id")
    )
    result["valid"] = not result["issues"]
    return result


def _accepted_clause_records(
    clause_output: StageOutput, clause_ids: list[str]
) -> list[dict[str, Any]]:
    if clause_output.stage != StageOutput.Stage.CLAUSE_PARSER or clause_output.status != StageOutput.Status.ACCEPTED:
        raise OccurrenceGenerationError(
            "accepted_clause_required", "Occurrence generation requires accepted Clause Parser output"
        )
    payload = clause_output.payload or {}
    source = payload.get("clauses") or []
    by_id = {str(row.get("clause_id") or "").zfill(3): row for row in source if isinstance(row, dict)}
    selected_ids = []
    for value in clause_ids:
        selected_ids.extend(part.strip().zfill(3) for part in str(value).split(",") if part.strip())
    if not selected_ids:
        raise OccurrenceGenerationError("clause_ids_required", "Select at least one Clause")
    missing = [value for value in selected_ids if value not in by_id]
    if missing:
        raise OccurrenceGenerationError("clause_not_found", f"Unknown Clause IDs: {', '.join(missing)}")
    return [
        {
            "clause_id": value,
            "sequence": int(by_id[value].get("sequence") or index + 1),
            "text": str(by_id[value].get("text") or ""),
        }
        for index, value in enumerate(selected_ids)
    ]


def _approved_entity_package(
    clause_output: StageOutput, entity_output: StageOutput
) -> dict[str, Any]:
    if clause_output.document_id != entity_output.document_id:
        raise OccurrenceGenerationError(
            "upstream_document_mismatch", "Clause and Entity outputs belong to different documents"
        )
    if not entity_downstream_is_eligible(entity_output):
        raise OccurrenceGenerationError(
            "approved_entity_required",
            "Occurrence generation requires the per-row human-reviewed Entity downstream package",
        )
    return build_entity_downstream_package(entity_output)


def _load_resources() -> dict[str, dict[str, Any]]:
    resources = {}
    for name, path in RESOURCE_PATHS.items():
        if not path.exists():
            raise OccurrenceGenerationError("resource_missing", f"Missing Occurrence resource: {path.name}")
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        resources[name] = {
            "path": path,
            "text": text,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "characters": len(text),
        }
    return resources


def _load_event_candidates(
    path: Path, *, clause_ids: set[str]
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for case in payload.get("cases") or []:
        clause_id = str(case.get("clause_id") or "").zfill(3)
        if clause_id not in clause_ids:
            continue
        candidates = ((case.get("strategies") or {}).get("hybrid") or {}).get("top_20") or []
        cases.append(
            {
                "event_cut_id": str(case.get("event_cut_id") or ""),
                "clause_id": clause_id,
                "event_cut_text": str(case.get("event_cut_text") or ""),
                "candidates": [
                    {
                        "rank": candidate.get("rank"),
                        "event_id": candidate.get("event_id"),
                        "headword": candidate.get("headword"),
                        "score": candidate.get("score"),
                    }
                    for candidate in candidates
                ],
            }
        )
    return cases


def _bounded_authority(
    *,
    clauses: list[dict[str, Any]],
    entity_package: dict[str, Any],
    event_assignments: list[dict[str, Any]],
    resources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    event_rows = _csv_rows(resources["events"]["text"])
    attribute_rows = _csv_rows(resources["attributes"]["text"])
    social_rows = _csv_rows(resources["social_identities"]["text"])
    quantified_rows = _csv_rows(resources["quantified_statements"]["text"])
    event_ids = {str(assignment.get("event_id") or "") for assignment in event_assignments}
    selected_events = [row for row in event_rows if row.get("ID") in event_ids]
    official_by_id = {str(row.get("ID") or ""): row for row in event_rows}
    for assignment in event_assignments:
        official = official_by_id.get(str(assignment.get("event_id") or ""))
        if official is None and str(assignment.get("event_id") or "").startswith("NEW-E-"):
            synthetic = {
                "ID": str(assignment.get("event_id") or ""),
                "Headword": str(assignment.get("headword") or ""),
                "Definition": "Historian-confirmed provisional Event headword",
            }
            selected_events.append(synthetic)
            continue
        if official is None:
            raise OccurrenceGenerationError(
                "assigned_event_not_in_authority",
                f"Accepted Event ID is not in Events_List.csv: {assignment.get('event_id')}",
            )
        if _normalized(official.get("Headword")) != _normalized(assignment.get("headword")):
            raise OccurrenceGenerationError(
                "assigned_event_headword_mismatch",
                f"Accepted Event ID/headword does not match authority: {assignment.get('event_id')} / {assignment.get('headword')}",
            )
    query_text = " ".join(row["text"] for row in clauses)
    selected_attributes = _lexical_rows(attribute_rows, query_text, limit=10)
    entity_ids = {
        str(row.get("id") or "")
        for row in entity_package["reviewed_rows"]
        if str(row.get("id") or "").startswith("SI-")
    }
    for row in entity_package["reviewed_rows"]:
        for field in row.get("fields") or []:
            entity_ids.update(
                reference
                for reference in field.get("referenced_ids") or []
                if str(reference).startswith("SI-")
            )
    selected_social = [row for row in social_rows if row.get("ID") in entity_ids]
    allowed_ids = {
        str(row.get("ID") or "")
        for row in event_rows + attribute_rows + quantified_rows
        if row.get("ID")
    }
    allowed_ids.update(
        str(assignment.get("event_id") or "")
        for assignment in event_assignments
        if str(assignment.get("event_id") or "").startswith("NEW-E-")
    )
    authority_headwords = {
        str(row.get("ID") or ""): str(row.get("Headword") or "")
        for row in event_rows + attribute_rows + quantified_rows
        if row.get("ID") and row.get("Headword")
    }
    authority_headwords.update({
        str(assignment.get("event_id") or ""): str(assignment.get("headword") or "")
        for assignment in event_assignments
        if str(assignment.get("event_id") or "").startswith("NEW-E-")
    })
    return {
        "event_assignments": event_assignments,
        "event_rows": selected_events,
        "attribute_rows": selected_attributes,
        "social_rows": selected_social,
        "quantified_rows": quantified_rows,
        "allowed_ids": allowed_ids,
        "authority_headwords": authority_headwords,
        "provenance": {
            "selection_contract": "occurrence-bounded-authority-v1",
            "all_resources_fully_indexed": True,
            "events_total": len(event_rows),
            "events_model_visible": len(selected_events),
            "attributes_total": len(attribute_rows),
            "attributes_model_visible": len(selected_attributes),
            "social_identities_total": len(social_rows),
            "social_identities_model_visible": len(selected_social),
            "quantified_statements_total": len(quantified_rows),
            "quantified_statements_model_visible": len(quantified_rows),
            "event_candidate_source": "human-accepted EventCut headword assignments",
            "event_candidates_model_visible_per_eventcut": 1,
            "occurrence_may_reselect_event_headword": False,
            "lexical_fallback_used_for_events": False,
            "output_ids_validated_against_complete_authority": True,
        },
    }


def _build_prompt(
    *,
    resources: dict[str, dict[str, Any]],
    clauses: list[dict[str, Any]],
    entity_package: dict[str, Any],
    assignment_package: dict[str, Any],
    authority: dict[str, Any],
) -> tuple[str, str]:
    system_prompt = resources["system_prompt"]["text"]
    clause_text = "\n\n".join(
        f"CLAUSE {row['clause_id']}\n\n{row['text']}" for row in clauses
    )
    entity_text = "\n".join(
        str(row.get("raw_line") or _entity_row_text(row))
        for row in entity_package["reviewed_rows"]
    )
    event_assignment_text = json.dumps(
        assignment_package["assignments"], ensure_ascii=False, indent=2
    )
    user_prompt = f"""===== GOVERNING INSTRUCTIONS =====
{resources['instructions']['text']}

===== LEGAL BOILERPLATE =====
{resources['legal_boilerplate']['text']}

===== BOUNDED EVENT AUTHORITY FROM FULLY INDEXED Events_List.csv =====
{_rows_as_csv(authority['event_rows'])}

===== HUMAN-APPROVED EVENTCUT HEADWORD ASSIGNMENTS =====
These mappings are authoritative. Emit exactly one E tag for each assignment. Use its exact Event ID and Headword. Do not choose or invent a different Event headword.
{event_assignment_text}

===== BOUNDED ATTRIBUTE AUTHORITY FROM FULLY INDEXED Attributes_List.csv =====
{_rows_as_csv(authority['attribute_rows'])}

===== APPROVED SOCIAL IDENTITY AUTHORITY =====
{_rows_as_csv(authority['social_rows'])}

===== COMPLETE QUANTIFIED STATEMENTS AUTHORITY =====
{_rows_as_csv(authority['quantified_rows'])}

===== HUMAN-APPROVED ENTITY REGISTRY =====
Only these accepted or edited Entity rows may be referenced. Rejected rows are absent.
{entity_text}

===== ACCEPTED CLAUSES TO TAG =====
{clause_text}

Produce only these Clause blocks in the same order. Reproduce each Clause text, then add E:, A:, and Q: lines. Every tag requires an exact Trigger substring. For E tags, enrich only the human-approved assignments above with Actor, Counterparty, Object, When, Location, and other supported fields. Do not emit any additional E tag. A: and Q: may use their bounded authorities or NEW only when no supplied authority fits. Return no commentary."""
    return system_prompt, user_prompt


def _provider_payload(
    *,
    request_id: str,
    document_id: str,
    system_prompt: str,
    user_prompt: str,
    clauses: list[dict[str, Any]],
    entity_package: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    return {
        "schema_version": "emtl-stage-execution-request-v1",
        "contract_version": "emtl-stage-contract-v1",
        "payload_schema_version": "emtl-provider-api-payload-draft-v1",
        "request_id": request_id,
        "stage_id": "occurrences_registry",
        "stage_label": "Occurrences Registry",
        "provider": "gpu_local",
        "document_id": document_id,
        "document_title": document_id,
        "document_type": "historical_document",
        "required_stage_ids": ["clause_parser", "entity_registry"],
        "accepted_upstream_stage_ids": ["clause_parser", "entity_registry"],
        "correction_requested": False,
        "inputs": {
            "document_header": f"DocID: {document_id}\n<END>",
            "document_body": "\n\n".join(row["text"] for row in clauses),
            "upstream_outputs": {
                "entity_registry": {
                    "status": "accepted",
                    "payload": entity_package,
                }
            },
        },
        "prompt_package": {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prompt_character_count": len(system_prompt) + len(user_prompt),
            "loaded_files": [],
            "source_completeness": {
                "source_complete": True,
                "truncation_applied": False,
            },
        },
        "options": {
            "timeout_seconds": 3600,
            "max_output_tokens": max_output_tokens,
        },
    }


def _csv_rows(text: str) -> list[dict[str, str]]:
    return [dict(row) for row in csv.DictReader(text.splitlines())]


def _lexical_rows(rows: list[dict[str, str]], query: str, *, limit: int) -> list[dict[str, str]]:
    query_tokens = set(TOKEN.findall(query.casefold()))
    scored = []
    for row in rows:
        text = " ".join(str(value or "") for value in row.values()).casefold()
        row_tokens = set(TOKEN.findall(text))
        score = len(query_tokens & row_tokens)
        if score:
            scored.append((-score, str(row.get("ID") or ""), row))
    return [row for _, _, row in sorted(scored)[:limit]]


def _rows_as_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no matching authority rows)"
    headers = list(rows[0])
    lines = [",".join(headers)]
    for row in rows:
        lines.append(json.dumps([row.get(header, "") for header in headers], ensure_ascii=False))
    return "\n".join(lines)


def _parse_fields(value: str) -> dict[str, str]:
    fields = {}
    for part in value.split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, field_value = part.split(":", 1)
        fields[key.strip()] = field_value.strip()
    return fields


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_clause_id(value: Any) -> str:
    return str(value or "").strip().zfill(3)


def _issue(code: str, clause_id: str, message: str, raw_line: str = "") -> dict[str, Any]:
    return {"code": code, "clause_id": clause_id, "message": message, "raw_line": raw_line}


def _entity_row_text(row: dict[str, Any]) -> str:
    fields = " | ".join(
        f"{field.get('key')}: {field.get('value')}" for field in row.get("fields") or []
    )
    return f"{row.get('type')}: {row.get('headword')} [{row.get('id')}] | {fields}"


def _resource_audit(resources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "path": str(data["path"]),
            "sha256": data["sha256"],
            "characters": data["characters"],
            "indexed_complete": True,
            "truncated": False,
        }
        for role, data in resources.items()
    ]
