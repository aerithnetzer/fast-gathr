from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from django.db import IntegrityError, transaction

from tagger.models import Document, StageExecutionAttempt, StageOutput

from .contracts import ExecutionStatus, ProviderApiPayload, StageExecutionRequest, now_iso
from .providers.factory import StageGenerationClient, stage_generation_client


INTERNAL_CONTRACT_VERSION = "eventcut-extraction-internal-v1"
DOWNSTREAM_CONTRACT_VERSION = "eventcut-downstream-v1"
LLM_OUTPUT_CONTRACT_VERSION = "eventcut-llm-output-v1"
EVENTCUT_STAGE_ID = "eventcut_extraction"
EVENTCUT_MODEL = "Qwen2.5-32B-Instruct"


class EventCutExtractionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_accepted_clause_output(
    *, doc_id: str = "", clause_stage_output_id: int | None = None
) -> StageOutput:
    cleaned_doc_id = str(doc_id or "").strip()
    if not cleaned_doc_id and clause_stage_output_id is None:
        raise EventCutExtractionError(
            "source_required", "Provide --doc-id and/or --clause-stage-output-id."
        )
    query = StageOutput.objects.select_related("document").filter(
        stage=StageOutput.Stage.CLAUSE_PARSER,
        status=StageOutput.Status.ACCEPTED,
    )
    if clause_stage_output_id is not None:
        query = query.filter(pk=clause_stage_output_id)
    if cleaned_doc_id:
        query = query.filter(document__doc_id=cleaned_doc_id)
    output = query.first()
    if output is None:
        qualifier = (
            f"StageOutput {clause_stage_output_id}"
            if clause_stage_output_id is not None
            else f"document {cleaned_doc_id}"
        )
        raise EventCutExtractionError(
            "accepted_clause_parser_required",
            f"No accepted Clause Parser output exists for {qualifier}.",
        )
    return output


def clause_records_from_output(
    clause_output: StageOutput, selected_clause_ids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    raw_clauses = clause_output.payload.get("clauses")
    if not isinstance(raw_clauses, list):
        raise EventCutExtractionError(
            "clause_payload_missing",
            "Accepted Clause Parser payload does not contain a clauses list.",
        )
    clauses: list[dict[str, Any]] = []
    for index, item in enumerate(raw_clauses, start=1):
        if not isinstance(item, dict):
            continue
        clause_id = str(item.get("clause_id") or "").strip()
        text = str(item.get("text") or "")
        if not clause_id or not text:
            continue
        clauses.append(
            {
                "clause_id": clause_id,
                "sequence": int(item.get("sequence") or index),
                "text": text,
                "text_sha256": _sha(text),
            }
        )
    requested = _clean_clause_ids(selected_clause_ids or [])
    if requested:
        by_id = {item["clause_id"]: item for item in clauses}
        missing = [clause_id for clause_id in requested if clause_id not in by_id]
        if missing:
            raise EventCutExtractionError(
                "clause_ids_not_found",
                "Selected Clause IDs do not exist in the accepted output: "
                + ", ".join(missing),
            )
        clauses = [by_id[clause_id] for clause_id in requested]
        clauses.sort(key=lambda item: item["sequence"])
    if not clauses:
        raise EventCutExtractionError(
            "no_source_clauses", "No usable clauses were found in the accepted output."
        )
    return clauses


def build_eventcut_prompt(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    system_prompt = """You extract EventCuts from accepted historical-text Clauses.

An EventCut is the exact source-text segment for one event-bearing action or expression. Follow every rule:
- Make an EventCut smaller than a Clause when the Clause contains multiple Events.
- Make it larger than a bare verb when local context is needed to identify the action.
- Copy exact source text from its Clause, preserving historical spelling, punctuation, and capitalization.
- Never modernize spelling and never use the whole Clause by default.
- Do not emit an isolated ambiguous verb.
- A Clause may yield zero, one, or multiple EventCuts; each cut targets one event-bearing action/expression.
- Keep wider disambiguating context in lookup_context_text, separate from event_cut_text.
- Do not treat a purely descriptive noun phrase as an Event solely because it names a gift or payment.
- Do not assign an Event headword, E-ID, candidate, score, or Occurrence.

Return JSON only, using contract_version eventcut-llm-output-v1 and an event_cuts array. Each item has clause_id, event_cut_text, optional trigger, optional lookup_context_text, and optional ambiguity_context_note. If trigger is present, copy it exactly from event_cut_text. If lookup_context_text is present, copy it exactly from the Clause."""
    clause_payload = [
        {"clause_id": item["clause_id"], "text": item["text"]}
        for item in clauses
    ]
    user_prompt = (
        "Extract EventCuts from only these accepted Clauses, in narrative order. "
        "Return no commentary and no Event headword IDs.\n\n"
        + json.dumps({"clauses": clause_payload}, ensure_ascii=False, indent=2)
        + "\n\nRequired shape:\n"
        + json.dumps(
            {
                "contract_version": LLM_OUTPUT_CONTRACT_VERSION,
                "event_cuts": [
                    {
                        "clause_id": "003",
                        "event_cut_text": "exact source span",
                        "trigger": "exact action phrase",
                        "lookup_context_text": "optional exact wider source span",
                        "ambiguity_context_note": "optional concise note",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "prompt_character_count": len(system_prompt) + len(user_prompt),
        "source_clause_count": len(clauses),
        "source_clause_ids": [item["clause_id"] for item in clauses],
        "prompt_hashes": {
            "system_prompt_sha256": _sha(system_prompt),
            "user_prompt_sha256": _sha(user_prompt),
        },
    }


def parse_eventcut_output(raw_output: str) -> list[dict[str, Any]]:
    text = str(raw_output or "").strip()
    if not text:
        raise ValueError("EventCut output is empty.")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    starts = [position for position in (text.find("{"), text.find("[")) if position >= 0]
    if not starts:
        raise ValueError("EventCut output does not contain JSON.")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
    except json.JSONDecodeError as exc:
        raise ValueError(f"EventCut output is not valid JSON: {exc}") from exc
    if isinstance(parsed, list):
        event_cuts = parsed
    elif isinstance(parsed, dict):
        version = str(parsed.get("contract_version") or "")
        if version and version != LLM_OUTPUT_CONTRACT_VERSION:
            raise ValueError(f"Unsupported EventCut output contract: {version}")
        event_cuts = parsed.get("event_cuts")
    else:
        event_cuts = None
    if not isinstance(event_cuts, list):
        raise ValueError("EventCut output must contain an event_cuts array.")
    if not all(isinstance(item, dict) for item in event_cuts):
        raise ValueError("Every EventCut must be a JSON object.")
    return [dict(item) for item in event_cuts]


def validate_eventcuts(
    event_cuts: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    *,
    doc_id: str,
    source_stage_output_id: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    clause_map = {item["clause_id"]: item for item in clauses}
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for original_index, item in enumerate(event_cuts):
        clause_id = str(item.get("clause_id") or "").strip()
        event_text = str(item.get("event_cut_text") or "").strip()
        trigger_supplied = "trigger" in item and item.get("trigger") is not None
        trigger = str(item.get("trigger") or "").strip()
        context_supplied = (
            "lookup_context_text" in item and item.get("lookup_context_text") is not None
        )
        lookup_context = str(item.get("lookup_context_text") or "").strip()
        note = str(
            item.get("ambiguity_context_note")
            or item.get("ambiguity_note")
            or item.get("context_note")
            or ""
        ).strip()
        issues: list[dict[str, str]] = []
        clause = clause_map.get(clause_id)
        offset = -1
        if clause is None:
            issues.append(_issue("unknown_clause_id", f"Clause ID does not exist: {clause_id or '<empty>'}"))
        if not event_text:
            issues.append(_issue("empty_event_cut_text", "event_cut_text must be non-empty."))
        elif clause is not None:
            offset = clause["text"].find(event_text)
            if offset < 0:
                issues.append(_issue("event_cut_not_exact_substring", "event_cut_text is not an exact substring of its Clause."))
        if trigger_supplied and not trigger:
            issues.append(_issue("empty_trigger", "trigger must be non-empty when supplied."))
        elif trigger and trigger not in event_text:
            issues.append(_issue("trigger_not_in_event_cut", "trigger is not an exact substring of event_cut_text."))
        if context_supplied and not lookup_context:
            issues.append(_issue("empty_lookup_context", "lookup_context_text must be non-empty when supplied."))
        elif lookup_context and clause is not None and lookup_context not in clause["text"]:
            issues.append(_issue("lookup_context_not_in_clause", "lookup_context_text is not an exact substring of its Clause."))
        duplicate_key = (clause_id, event_text)
        if event_text and duplicate_key in seen_keys:
            issues.append(_issue("duplicate_event_cut", "Duplicate EventCut span in the same Clause."))
        seen_keys.add(duplicate_key)
        event_cut_id = _event_cut_id(doc_id, clause_id, event_text)
        normalized.append(
            {
                "event_cut_id": event_cut_id,
                "doc_id": doc_id,
                "clause_id": clause_id,
                "clause_sequence": clause["sequence"] if clause else None,
                "clause_text": clause["text"] if clause else "",
                "clause_text_sha256": clause["text_sha256"] if clause else "",
                "event_cut_text": event_text,
                "trigger": trigger,
                "lookup_context_text": lookup_context,
                "ambiguity_context_note": note,
                "source_offsets": (
                    {"start": offset, "end": offset + len(event_text)} if offset >= 0 else None
                ),
                "source_clause_parser_stage_output_id": source_stage_output_id,
                "valid": not issues,
                "validation_issues": issues,
                "_original_index": original_index,
            }
        )
    original_ids = [item["event_cut_id"] for item in normalized]
    normalized.sort(
        key=lambda item: (
            item["clause_sequence"] if item["clause_sequence"] is not None else 10**9,
            (item["source_offsets"] or {}).get("start", 10**9),
            item["_original_index"],
        )
    )
    sorted_ids = [item["event_cut_id"] for item in normalized]
    reordered = original_ids != sorted_ids
    all_issues = [
        {"event_cut_id": item["event_cut_id"], **issue}
        for item in normalized
        for issue in item["validation_issues"]
    ]
    for item in normalized:
        item.pop("_original_index", None)
    report = {
        "valid": not all_issues,
        "requires_user_review": False,
        "event_cut_count": len(normalized),
        "valid_event_cut_count": sum(bool(item["valid"]) for item in normalized),
        "invalid_event_cut_count": sum(not bool(item["valid"]) for item in normalized),
        "narrative_order_preserved": True,
        "llm_output_reordered_to_narrative_order": reordered,
        "issues": all_issues,
        "cut_results": [
            {
                "event_cut_id": item["event_cut_id"],
                "clause_id": item["clause_id"],
                "valid": item["valid"],
                "issues": list(item["validation_issues"]),
            }
            for item in normalized
        ],
    }
    return normalized, report


def build_internal_package(
    *,
    document: Document,
    clause_output: StageOutput,
    clauses: list[dict[str, Any]],
    raw_output: str,
    provider: str,
    model: str,
    request_id: str,
    provider_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        parsed = parse_eventcut_output(raw_output)
        cuts, report = validate_eventcuts(
            parsed,
            clauses,
            doc_id=document.doc_id,
            source_stage_output_id=int(clause_output.pk),
        )
    except ValueError as exc:
        cuts = []
        report = {
            "valid": False,
            "requires_user_review": False,
            "event_cut_count": 0,
            "valid_event_cut_count": 0,
            "invalid_event_cut_count": 0,
            "narrative_order_preserved": False,
            "llm_output_reordered_to_narrative_order": False,
            "issues": [_issue("eventcut_output_malformed", str(exc))],
            "cut_results": [],
        }
    return {
        "contract_version": INTERNAL_CONTRACT_VERSION,
        "doc_id": document.doc_id,
        "source_clause_parser_stage_output_id": int(clause_output.pk),
        "source_clauses": [
            {
                "clause_id": item["clause_id"],
                "sequence": item["sequence"],
                "text_sha256": item["text_sha256"],
            }
            for item in clauses
        ],
        "raw_llm_output": raw_output,
        "parsed_event_cuts": cuts,
        "validation_report": report,
        "provider_provenance": {
            "provider": provider,
            "model": model,
            "request_id": request_id,
            "generated_at": now_iso(),
            "metadata": dict(provider_metadata or {}),
        },
        "internal_usable_for_lookup": bool(report["valid"]),
    }


def build_downstream_package(stage_output: StageOutput) -> dict[str, Any]:
    if stage_output.stage != StageOutput.Stage.EVENTCUT_EXTRACTION:
        raise EventCutExtractionError(
            "eventcut_stage_output_required", "Selected StageOutput is not EventCut extraction."
        )
    payload = dict(stage_output.payload or {})
    if payload.get("contract_version") != INTERNAL_CONTRACT_VERSION:
        raise EventCutExtractionError(
            "eventcut_contract_invalid", "StageOutput does not use the internal EventCut contract."
        )
    if payload.get("internal_usable_for_lookup") is not True:
        raise EventCutExtractionError(
            "eventcut_package_not_usable", "EventCut package is not validated for lookup."
        )
    valid_cuts = [
        item
        for item in payload.get("parsed_event_cuts", [])
        if isinstance(item, dict) and item.get("valid") is True
    ]
    exported = [
        {
            key: item.get(key)
            for key in (
                "event_cut_id",
                "doc_id",
                "clause_id",
                "event_cut_text",
                "trigger",
                "lookup_context_text",
                "ambiguity_context_note",
                "clause_text",
                "clause_text_sha256",
                "clause_sequence",
                "source_offsets",
                "source_clause_parser_stage_output_id",
            )
        }
        for item in valid_cuts
    ]
    seed = json.dumps(exported, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "contract_version": DOWNSTREAM_CONTRACT_VERSION,
        "package_id": "eventcut-downstream-" + _sha(seed)[:24],
        "doc_id": payload.get("doc_id") or stage_output.document.doc_id,
        "source_eventcut_stage_output_id": int(stage_output.pk),
        "source_clause_parser_stage_output_id": payload.get(
            "source_clause_parser_stage_output_id"
        ),
        "event_cut_count": len(exported),
        "event_cuts": exported,
    }


def write_json_package(package: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class EventCutExtractionRunner:
    def __init__(self, *, client: StageGenerationClient | None = None) -> None:
        self.client = client or stage_generation_client()

    def run(
        self,
        *,
        clause_output: StageOutput,
        clauses: list[dict[str, Any]],
        confirm_real_generation: bool,
        request_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not confirm_real_generation:
            raise EventCutExtractionError(
                "real_generation_confirmation_required",
                "EventCut extraction requires --confirm-real-generation.",
            )
        if (
            clause_output.stage != StageOutput.Stage.CLAUSE_PARSER
            or clause_output.status != StageOutput.Status.ACCEPTED
        ):
            raise EventCutExtractionError(
                "accepted_clause_parser_required",
                "EventCut extraction requires an accepted Clause Parser StageOutput.",
            )
        document = clause_output.document
        cleaned_request_id = str(request_id or "").strip() or f"eventcut-real-{uuid4().hex}"
        if StageExecutionAttempt.objects.filter(request_id=cleaned_request_id).exists():
            raise EventCutExtractionError(
                "duplicate_eventcut_request",
                f"This EventCut request ID already exists: {cleaned_request_id}",
            )
        prompt = build_eventcut_prompt(clauses)
        request = StageExecutionRequest(
            stage_id=EVENTCUT_STAGE_ID,
            stage_label="EventCut Extraction (internal)",
            provider="gpu_local",
            requested_provider="gpu_local",
            document_id=document.doc_id,
            document_title=document.title,
            document_type=document.document_type,
            request_id=cleaned_request_id,
            required_stage_ids=(StageOutput.Stage.CLAUSE_PARSER,),
            accepted_upstream_stage_ids=(StageOutput.Stage.CLAUSE_PARSER,),
            source_character_count=sum(len(item["text"]) for item in clauses),
            prompt_character_count=prompt["prompt_character_count"],
            metadata={
                "operation": "eventcut_internal_extraction",
                "source_clause_parser_stage_output_id": clause_output.pk,
            },
        )
        provider_payload = ProviderApiPayload(
            request=request,
            inputs={
                "source_clause_parser_stage_output_id": clause_output.pk,
                "clauses": [
                    {"clause_id": item["clause_id"], "text": item["text"]}
                    for item in clauses
                ],
            },
            prompt_package=prompt,
            options={
                "operation": "eventcut_internal_extraction",
                "generation_enabled": True,
                "tokenization_only": False,
                "max_output_tokens": 4096,
            },
        )
        stage_output, _ = StageOutput.objects.get_or_create(
            document=document,
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION,
            defaults={
                "status": StageOutput.Status.NOT_STARTED,
                "display_title": "EventCut Extraction (internal)",
            },
        )
        try:
            attempt = StageExecutionAttempt.objects.create(
                stage_output=stage_output,
                request_id=cleaned_request_id,
                stage=EVENTCUT_STAGE_ID,
                execution_status="generation_pending",
                disposition=StageExecutionAttempt.Disposition.RECORDED_ONLY,
                provider="gpu_local",
                model=EVENTCUT_MODEL,
                provenance={
                    "contract_version": INTERNAL_CONTRACT_VERSION,
                    "request": request.as_dict(),
                    "prompt_hashes": prompt["prompt_hashes"],
                    "source_clause_parser_stage_output_id": clause_output.pk,
                },
                validation={"valid": False},
            )
        except IntegrityError as exc:
            raise EventCutExtractionError(
                "duplicate_eventcut_request",
                f"This EventCut request ID already exists: {cleaned_request_id}",
            ) from exc
        response = self.client.generate(provider_payload.as_dict())
        if response.status == ExecutionStatus.COMPLETED.value:
            package = build_internal_package(
                document=document,
                clause_output=clause_output,
                clauses=clauses,
                raw_output=response.raw_output,
                provider=response.provider,
                model=response.model,
                request_id=cleaned_request_id,
                provider_metadata=response.metadata,
            )
        else:
            package = {
                "contract_version": INTERNAL_CONTRACT_VERSION,
                "doc_id": document.doc_id,
                "source_clause_parser_stage_output_id": int(clause_output.pk),
                "source_clauses": [
                    {
                        "clause_id": item["clause_id"],
                        "sequence": item["sequence"],
                        "text_sha256": item["text_sha256"],
                    }
                    for item in clauses
                ],
                "raw_llm_output": response.raw_output,
                "parsed_event_cuts": [],
                "validation_report": {
                    "valid": False,
                    "requires_user_review": False,
                    "issues": list(response.errors or [])
                    or [_issue("provider_generation_failed", response.error or "Provider generation failed.")],
                },
                "provider_provenance": {
                    "provider": response.provider,
                    "model": response.model,
                    "request_id": cleaned_request_id,
                    "generated_at": now_iso(),
                    "metadata": dict(response.metadata or {}),
                },
                "internal_usable_for_lookup": False,
            }
        self._persist(
            stage_output=stage_output,
            attempt=attempt,
            response=response,
            package=package,
            prompt=prompt,
        )
        summary = {
            "contract_version": INTERNAL_CONTRACT_VERSION,
            "request_id": cleaned_request_id,
            "attempt_id": attempt.pk,
            "stage_output_id": stage_output.pk,
            "source_clause_parser_stage_output_id": clause_output.pk,
            "doc_id": document.doc_id,
            "selected_clause_ids": [item["clause_id"] for item in clauses],
            "execution_status": response.status,
            "internal_usable_for_lookup": package["internal_usable_for_lookup"],
            "event_cut_count": len(package.get("parsed_event_cuts") or []),
            "validation_issue_count": len(
                (package.get("validation_report") or {}).get("issues") or []
            ),
            "clause_parser_stage_output_modified": False,
        }
        return summary, package

    @staticmethod
    @transaction.atomic
    def _persist(*, stage_output, attempt, response, package, prompt) -> None:
        locked_stage = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
        locked_attempt = StageExecutionAttempt.objects.select_for_update().get(pk=attempt.pk)
        usable = bool(package.get("internal_usable_for_lookup"))
        locked_attempt.execution_status = response.status
        locked_attempt.disposition = StageExecutionAttempt.Disposition.INTERNAL_APPLIED
        locked_attempt.provider = response.provider
        locked_attempt.model = response.model
        locked_attempt.raw_output = response.raw_output
        locked_attempt.payload = package
        locked_attempt.provenance = {
            **dict(locked_attempt.provenance or {}),
            "provider_metadata": dict(response.metadata or {}),
            "prompt_hashes": prompt["prompt_hashes"],
        }
        locked_attempt.validation = dict(package.get("validation_report") or {})
        locked_attempt.error = response.error
        locked_attempt.applied_to_stage_output = True
        locked_attempt.save()
        locked_stage.payload = package
        locked_stage.raw_output = response.raw_output
        locked_stage.provenance = {
            "contract_version": INTERNAL_CONTRACT_VERSION,
            "latest_attempt_id": locked_attempt.pk,
            "provider": response.provider,
            "model": response.model,
            "request_id": locked_attempt.request_id,
            "prompt_hashes": prompt["prompt_hashes"],
            "source_clause_parser_stage_output_id": package.get(
                "source_clause_parser_stage_output_id"
            ),
            "no_user_review_required": True,
        }
        locked_stage.status = (
            StageOutput.Status.LOADED if usable else StageOutput.Status.BLOCKED
        )
        locked_stage.save(
            update_fields=["payload", "raw_output", "provenance", "status", "updated_at"]
        )


def _clean_clause_ids(values: Iterable[str] | str) -> list[str]:
    # Django's call_command() may pass an action="append" option as one string,
    # while argparse supplies a list during normal CLI execution. Normalize both
    # forms before splitting comma-separated selectors.
    raw_values: Iterable[str] = [values] if isinstance(values, str) else values
    cleaned: list[str] = []
    for value in raw_values:
        for part in str(value or "").split(","):
            clause_id = part.strip()
            if clause_id and clause_id not in cleaned:
                cleaned.append(clause_id)
    return cleaned


def _event_cut_id(doc_id: str, clause_id: str, event_text: str) -> str:
    return "eventcut-" + _sha(f"{doc_id}\0{clause_id}\0{event_text}")[:24]


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": "error"}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
