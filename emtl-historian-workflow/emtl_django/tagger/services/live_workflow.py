from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from django.db import transaction
from django.utils import timezone

from tagger.models import Clause, NewIdProposal, StageExecutionAttempt, StageOutput

from .clause_persistence import replace_document_clauses
from .contracts import ExecutionStatus
from .entity_generation import EntityControlledGenerationRunner
from .entity_review_handoff import REVIEW_CONTRACT
from .event_headword_review import (
    EvidenceProposalSimilarityBackend,
    JsonReviewRepository,
    apply_review_action,
    create_review_item,
)
from .eventcut_extraction import (
    EventCutExtractionRunner,
    clause_records_from_output,
)
from .event_occurrence_workflow import build_merged_event_occurrence_package
from .occurrence_generation import (
    OccurrenceGenerationService,
    validate_edited_occurrence_output,
)
from .providers.gpu_local import GpuLocalProviderClient
from .providers.factory import stage_generation_client
from .stage_runner import ChatbotStageRunner, _document_header_and_body
from .stage_validation import parse_clause_output_structure, validate_clause_coverage
from .summary_assembler_generation import (
    AssemblerGenerationService,
    SummaryKeywordsGenerationService,
    validate_occurrence_conservation,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DJANGO_ROOT = Path(__file__).resolve().parents[2]
SSH_CONFIG = Path(os.getenv("EMTL_REMOTE_SSH_CONFIG") or REPO_ROOT / "tools" / "robots_ssh_config")
QWEN3_TOOL = Path(os.getenv("EMTL_QWEN3_LOOKUP_TOOL") or REPO_ROOT / "tools" / "qwen3_event_lookup.py")
EVENT_WORKBOOK = REPO_ROOT / "Chatbot docs" / "Events_List_VectorLLM_v1.xlsx"
REMOTE_SSH_TARGET = os.getenv("EMTL_REMOTE_SSH_TARGET", "")
REMOTE_BASE = os.getenv("EMTL_REMOTE_MODEL_ROOT", "")
REMOTE_MODEL = os.getenv("EMTL_REMOTE_EMBEDDING_MODEL", "")
REMOTE_VENV = os.getenv("EMTL_REMOTE_PYTHON_ENV", "")
REMOTE_CACHE = os.getenv("EMTL_REMOTE_LOOKUP_CACHE", "")
REMOTE_PROVIDER_SCRIPT = os.getenv("EMTL_REMOTE_PROVIDER_SCRIPT", "")
REMOTE_PROVIDER_MODEL = os.getenv("EMTL_REMOTE_GENERATION_MODEL", "")
REMOTE_PROVIDER_LOG = os.getenv("EMTL_REMOTE_PROVIDER_LOG", "")


class LiveWorkflowError(RuntimeError):
    pass


def run_summary(stage_output: StageOutput) -> dict[str, Any]:
    result = SummaryKeywordsGenerationService().run(
        document=stage_output.document,
        request_id=f"live-summary-{uuid4().hex}",
    )
    _persist_simple_result(stage_output, result)
    return {"status": result.status, "validation": result.validation}


def run_clause(stage_output: StageOutput) -> dict[str, Any]:
    result = ChatbotStageRunner().run(
        stage_id=StageOutput.Stage.CLAUSE_PARSER,
        document=stage_output.document,
        stage_outputs={},
        provider="gpu_local",
    )
    if result.status != ExecutionStatus.COMPLETED.value:
        raise LiveWorkflowError(result.error or "Clause Parser did not complete")
    coverage = result.payload.get("coverage_validation") or {}
    if not coverage.get("valid"):
        raise LiveWorkflowError("Clause Parser output failed source coverage validation")
    stage_output.raw_output = result.raw_output
    stage_output.payload = result.payload
    stage_output.provenance = result.provenance
    stage_output.status = StageOutput.Status.CHECKING
    stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])
    replace_document_clauses(stage_output.document, list(result.payload.get("clauses") or []))
    return {"status": result.status, "clause_count": len(result.payload.get("clauses") or [])}


def run_entity(stage_output: StageOutput) -> dict[str, Any]:
    summary = EntityControlledGenerationRunner().run(
        document=stage_output.document,
        stage_output=stage_output,
        confirm_real_generation=True,
        request_id=f"live-entity-{uuid4().hex}",
        require_readiness_evidence=False,
    )
    if summary.get("lifecycle_status") != StageOutput.Status.CHECKING:
        errors = summary.get("errors") or []
        message = "; ".join(str(item.get("message") or item) for item in errors if item) if errors else ""
        raise LiveWorkflowError(message or "Entity Bot did not produce a reviewable output")
    return summary


def run_eventcuts(clause_output: StageOutput, clause_ids: list[str] | None = None) -> dict[str, Any]:
    clauses = clause_records_from_output(clause_output, clause_ids or [])
    summary, _ = EventCutExtractionRunner().run(
        clause_output=clause_output,
        clauses=clauses,
        confirm_real_generation=True,
        request_id=f"live-eventcut-{uuid4().hex}",
    )
    return summary


def run_dense_and_chooser(eventcut_output: StageOutput, *, top_k: int = 20) -> dict[str, Any]:
    existing_dense = (eventcut_output.payload or {}).get("dense_lookup") or {}
    dense = (
        existing_dense
        if _dense_lookup_matches_eventcuts(existing_dense, eventcut_output)
        else RemoteQwen3LookupRunner().run(eventcut_output)
    )
    payload = deepcopy(eventcut_output.payload or {})
    payload["dense_lookup"] = dense
    eventcut_output.payload = payload
    eventcut_output.status = StageOutput.Status.CHECKING
    eventcut_output.save(update_fields=["payload", "status", "updated_at"])
    client = stage_generation_client()
    review_items = {}
    for case in dense.get("cases") or []:
        candidates = list(((case.get("strategies") or {}).get("hybrid") or {}).get(f"top_{top_k}") or [])
        if not candidates:
            raise LiveWorkflowError(f"Dense lookup returned no top-{top_k} candidates")
        chooser = _choose_candidate(client, case, candidates, top_k)
        item_id = f"review-{case['event_cut_id']}"
        review_items[item_id] = create_review_item(
            item_id=item_id,
            document_id=eventcut_output.document.doc_id,
            clause_id=str(case.get("clause_id") or ""),
            event_cut_id=str(case.get("event_cut_id") or ""),
            event_cut_text=str(case.get("event_cut_text") or ""),
            candidates=candidates,
            chooser_output=chooser,
            authority_version=str((dense.get("provenance") or {}).get("workbook_sha256") or ""),
        )
    payload = deepcopy(eventcut_output.payload or {})
    payload["headword_review_store"] = {
        "contract_version": "event-headword-human-review-v1",
        "items": review_items,
    }
    eventcut_output.payload = payload
    eventcut_output.save(update_fields=["payload", "updated_at"])
    return {"event_cut_count": len(review_items), "top_k": top_k, "chooser_completed": True}


def _dense_lookup_matches_eventcuts(dense: dict[str, Any], eventcut_output: StageOutput) -> bool:
    cases = list(dense.get("cases") or [])
    cuts = [
        row for row in ((eventcut_output.payload or {}).get("parsed_event_cuts") or [])
        if row.get("valid") is True
    ]
    if not cases or len(cases) != len(cuts):
        return False
    dense_keys = {
        (str(row.get("event_cut_id") or ""), str(row.get("event_cut_text") or "").strip())
        for row in cases
    }
    cut_keys = {
        (
            str(row.get("event_cut_id") or ""),
            str(row.get("event_cut_text") or row.get("text") or "").strip(),
        )
        for row in cuts
    }
    return dense_keys == cut_keys


def apply_headword_action(
    eventcut_output: StageOutput, *, item_id: str, action: str, actor: str,
    candidate_rank: int | None = None, proposed_headword: str = "",
    definition_hint: str = "", reviewer_note: str = "",
) -> dict[str, Any]:
    payload = deepcopy(eventcut_output.payload or {})
    store = deepcopy(payload.get("headword_review_store") or {})
    items = store.get("items") or {}
    item = items.get(item_id)
    if not isinstance(item, dict):
        raise LiveWorkflowError("Unknown Event headword review item")
    similarity_backend = None
    if action == "submit_proposal":
        evidence = RemoteQwen3LookupRunner().proposal_similarity(
            eventcut_output, proposed_headword=proposed_headword, definition_hint=definition_hint
        )
        similarity_backend = EvidenceProposalSimilarityBackend(evidence)
    updated = apply_review_action(
        item,
        action=action,
        actor=actor,
        expected_revision=int(item.get("revision") or 0),
        candidate_rank=candidate_rank,
        proposed_headword=proposed_headword,
        definition_hint=definition_hint,
        reviewer_note=reviewer_note,
        similarity_backend=similarity_backend,
    )
    items[item_id] = updated
    store["items"] = items
    payload["headword_review_store"] = store
    eventcut_output.payload = payload
    sync_eventcut_review_status(eventcut_output)
    return updated


def accept_remaining_headwords(eventcut_output: StageOutput, *, actor: str) -> int:
    payload = deepcopy(eventcut_output.payload or {})
    store = deepcopy(payload.get("headword_review_store") or {})
    items = store.get("items") or {}
    count = 0
    for item_id, item in list(items.items()):
        if str(item.get("state") or "") != "llm_selected_candidate":
            continue
        items[item_id] = apply_review_action(
            item, action="accept", actor=actor,
            expected_revision=int(item.get("revision") or 0),
        )
        count += 1
    store["items"] = items
    payload["headword_review_store"] = store
    eventcut_output.payload = payload
    sync_eventcut_review_status(eventcut_output)
    return count


def sync_eventcut_review_status(eventcut_output: StageOutput) -> None:
    payload = deepcopy(eventcut_output.payload or {})
    items = ((payload.get("headword_review_store") or {}).get("items") or {})
    terminal_states = {"accepted_existing_headword", "provisional_headword_pending_review"}
    complete = bool(items) and all(
        isinstance(item, dict)
        and str(item.get("state") or "") in terminal_states
        and isinstance(item.get("assignment"), dict)
        and item["assignment"].get("status") == "accepted"
        for item in items.values()
    )
    accepted_assignments = []
    if complete:
        cuts = {
            str(row.get("event_cut_id") or ""): row
            for row in payload.get("parsed_event_cuts") or []
            if isinstance(row, dict) and row.get("valid") is True
        }
        for item in items.values():
            assignment = deepcopy(item["assignment"])
            cut = cuts.get(str(assignment.get("event_cut_id") or ""), {})
            accepted_assignments.append({
                **assignment,
                "clause_id": str(cut.get("clause_id") or item.get("clause_id") or "").zfill(3),
                "event_cut_text": str(cut.get("event_cut_text") or (item.get("event_cut") or {}).get("text") or ""),
                "review_item_id": str(item.get("item_id") or ""),
                "review_revision": int(item.get("revision") or 0),
            })
    payload["accepted_assignments"] = accepted_assignments
    payload["headword_review_complete"] = complete
    eventcut_output.payload = payload
    eventcut_output.status = StageOutput.Status.ACCEPTED if complete else StageOutput.Status.CHECKING
    eventcut_output.save(update_fields=["payload", "status", "updated_at"])


def run_occurrence(
    *, occurrence_output: StageOutput, clause_output: StageOutput,
    entity_output: StageOutput, eventcut_output: StageOutput,
) -> dict[str, Any]:
    sync_eventcut_review_status(eventcut_output)
    if eventcut_output.status != StageOutput.Status.ACCEPTED:
        raise LiveWorkflowError("Every Event headword must have a final human decision")
    store = (eventcut_output.payload or {}).get("headword_review_store") or {}
    clause_ids = sorted({
        str(cut.get("clause_id") or "").zfill(3)
        for cut in (eventcut_output.payload or {}).get("parsed_event_cuts") or []
        if cut.get("valid") is True
    })
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "review.json"
        path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        result = OccurrenceGenerationService().run(
            clause_output=clause_output,
            entity_output=entity_output,
            eventcut_output=eventcut_output,
            clause_ids=clause_ids,
            event_review_store_path=path,
            request_id=f"live-occurrence-{uuid4().hex}",
            max_output_tokens=4096,
        )
    _persist_simple_result(occurrence_output, result)
    return {"status": result.status, "validation": result.validation}


def run_assembler(
    *, assembler_output: StageOutput, clause_output: StageOutput,
    entity_output: StageOutput, occurrence_output: StageOutput,
) -> dict[str, Any]:
    result = AssemblerGenerationService().run(
        clause_output=clause_output,
        entity_output=entity_output,
        occurrence_output=occurrence_output,
        request_id=f"live-assembler-{uuid4().hex}",
        conservation_test=False,
        max_output_tokens=4096,
    )
    _persist_simple_result(assembler_output, result)
    return {"status": result.status, "validation": result.validation}


@transaction.atomic
def save_entity_review_decision(
    stage_output: StageOutput, *, row_index: int, decision: str,
    edited_row: dict[str, Any] | None = None,
) -> None:
    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    tags = list(((locked.payload or {}).get("entity_output") or {}).get("tags") or [])
    if row_index < 0 or row_index >= len(tags):
        raise LiveWorkflowError("Unknown Entity row")
    if decision not in {"accepted", "edited", "rejected"}:
        raise LiveWorkflowError("Invalid Entity review decision")
    if decision == "edited":
        original = deepcopy(tags[row_index])
        selected = {**original, **deepcopy(edited_row or {})}
        prefix = f"{selected.get('type', '')}: {selected.get('headword', '')} [{selected.get('id', '')}]"
        original_line = str(original.get("raw_line") or "")
        suffix = original_line[original_line.find(" |"):] if " |" in original_line else ""
        selected["raw_line"] = prefix + suffix
        edited_row = selected
    payload = deepcopy(locked.payload or {})
    decisions = dict(payload.get("live_entity_decisions") or {})
    decisions[str(row_index)] = {
        "decision": decision,
        "edited_row": deepcopy(edited_row) if decision == "edited" else None,
        "updated_at": timezone.now().isoformat(),
    }
    payload["live_entity_decisions"] = decisions
    locked.payload = payload
    locked.save(update_fields=["payload", "updated_at"])


def accept_remaining_entities(stage_output: StageOutput) -> int:
    payload = deepcopy(stage_output.payload or {})
    tags = list((payload.get("entity_output") or {}).get("tags") or [])
    decisions = dict(payload.get("live_entity_decisions") or {})
    count = 0
    for index in range(len(tags)):
        if str(index) not in decisions:
            decisions[str(index)] = {"decision": "accepted", "edited_row": None, "updated_at": timezone.now().isoformat()}
            count += 1
    payload["live_entity_decisions"] = decisions
    stage_output.payload = payload
    stage_output.save(update_fields=["payload", "updated_at"])
    return count


@transaction.atomic
def finalize_entity_review(stage_output: StageOutput) -> None:
    locked = StageOutput.objects.select_for_update().get(pk=stage_output.pk)
    payload = deepcopy(locked.payload or {})
    tags = list((payload.get("entity_output") or {}).get("tags") or [])
    decisions = dict(payload.get("live_entity_decisions") or {})
    if len(decisions) != len(tags):
        raise LiveWorkflowError("Every Entity row must be reviewed before continuing")
    downstream = []
    review_rows = []
    for index, original in enumerate(tags):
        entry = decisions[str(index)]
        decision = entry["decision"]
        selected = deepcopy(entry.get("edited_row") or original)
        review_rows.append({
            "review_row_id": f"entity-{index + 1:04d}", "decision": decision,
            "original_row": original, "reviewed_row": None if decision == "rejected" else selected,
        })
        if decision != "rejected":
            downstream.append({**selected, "review_decision": decision})
    review = dict(payload.get("entity_review") or {})
    review.update({
        "contract_version": REVIEW_CONTRACT, "state": "approved",
        "approved_for_downstream": True, "reviewed_at": timezone.now().isoformat(),
        "review_rows": review_rows,
    })
    payload["entity_review"] = review
    payload["reviewed_entity_registry"] = downstream
    provenance = deepcopy(locked.provenance or {})
    entity_provenance = dict(provenance.get("entity_registry") or {})
    entity_provenance["approved_for_downstream"] = True
    provenance["entity_registry"] = entity_provenance
    locked.payload = payload
    locked.provenance = provenance
    locked.status = StageOutput.Status.ACCEPTED
    locked.save(update_fields=["payload", "provenance", "status", "updated_at"])


def accept_stage(stage_output: StageOutput) -> None:
    if stage_output.status != StageOutput.Status.CHECKING:
        raise LiveWorkflowError("Only checking output can be accepted")
    if stage_output.stage == StageOutput.Stage.ENTITY_REGISTRY:
        finalize_entity_review(stage_output)
        return
    stage_output.status = StageOutput.Status.ACCEPTED
    provenance = deepcopy(stage_output.provenance or {})
    provenance.update({"accepted_at": timezone.now().isoformat(), "accepted_via": "live_workflow_ui"})
    stage_output.provenance = provenance
    stage_output.save(update_fields=["status", "provenance", "updated_at"])


@transaction.atomic
def save_source_document(document: Any, source_text: str) -> None:
    cleaned = str(source_text or "").strip()
    if not cleaned:
        raise LiveWorkflowError("Source document cannot be empty")
    locked = type(document).objects.select_for_update().get(pk=document.pk)
    metadata = dict(locked.metadata or {})
    metadata.update({
        "working_source_text": cleaned,
        "working_source_text_source": "live_ui_manual_edit",
        "working_source_text_updated_at": timezone.now().isoformat(),
        "working_source_paragraph_count": len(_source_paragraphs(cleaned)),
    })
    locked.metadata = metadata
    locked.save(update_fields=["metadata", "updated_at"])
    _replace_document_paragraph_clauses(locked, cleaned)
    NewIdProposal.objects.filter(document=locked).delete()
    for output in locked.stage_outputs.select_for_update().all():
        output.raw_output = ""
        output.payload = {}
        output.provenance = {
            "reset_at": timezone.now().isoformat(),
            "reset_reason": "source_document_edited_in_live_ui",
        }
        output.status = StageOutput.Status.BLOCKED if output.stage == StageOutput.Stage.KEY_NARRATIVE else StageOutput.Status.NOT_STARTED
        output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])


def save_summary_output(stage_output: StageOutput, raw_output: str) -> None:
    text = str(raw_output or "").strip()
    if not text:
        raise LiveWorkflowError("Summary & Keywords output cannot be empty")
    payload = deepcopy(stage_output.payload or {})
    payload.update({
        "manual_review_text": text,
        "human_edited": True,
        "edited_at": timezone.now().isoformat(),
    })
    provenance = deepcopy(stage_output.provenance or {})
    provenance.update({"human_edited_at": timezone.now().isoformat(), "human_edit_source": "live_ui"})
    stage_output.raw_output = text
    stage_output.payload = payload
    stage_output.provenance = provenance
    stage_output.status = StageOutput.Status.CHECKING
    stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])


def save_clause_output(stage_output: StageOutput, raw_output: str) -> None:
    text = str(raw_output or "").strip()
    if not text:
        raise LiveWorkflowError("Clause Parser output cannot be empty")
    header, body = _document_header_and_body(stage_output.document)
    parsed = parse_clause_output_structure(text)
    coverage = validate_clause_coverage(
        body,
        parsed.clauses,
        expected_header=header,
        generated_header=parsed.generated_header,
    )
    if not coverage.valid:
        raise LiveWorkflowError(f"Edited Clause Parser output failed coverage validation: {coverage.message}")
    payload = deepcopy(stage_output.payload or {})
    payload.update(parsed.as_dict())
    payload["coverage_validation"] = coverage.as_dict()
    payload["human_edited"] = True
    payload["edited_at"] = timezone.now().isoformat()
    provenance = deepcopy(stage_output.provenance or {})
    provenance.update({"human_edited_at": timezone.now().isoformat(), "human_edit_source": "live_ui"})
    stage_output.raw_output = text
    stage_output.payload = payload
    stage_output.provenance = provenance
    stage_output.status = StageOutput.Status.CHECKING
    stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])
    replace_document_clauses(stage_output.document, list(parsed.clauses))


def save_occurrence_output(
    stage_output: StageOutput, raw_output: str, *,
    clause_output: StageOutput, entity_output: StageOutput,
) -> None:
    text = str(raw_output or "").strip()
    if not text:
        raise LiveWorkflowError("Occurrence Registry output cannot be empty")
    validation = validate_edited_occurrence_output(
        raw_output=text,
        clause_output=clause_output,
        entity_output=entity_output,
        occurrence_output=stage_output,
    )
    if not validation.get("valid"):
        issues = list(validation.get("issues") or [])
        messages = [str(item.get("message") or item.get("code") or item) for item in issues[:3]]
        raise LiveWorkflowError(
            "Edited Occurrence Registry output failed validation: " + "; ".join(messages)
        )
    payload = deepcopy(stage_output.payload or {})
    payload["validation"] = validation
    payload["parsed_clauses"] = validation.get("parsed_clauses") or []
    payload["human_edited"] = True
    payload["edited_at"] = timezone.now().isoformat()
    assignment_package = payload.get("event_assignment_package") or {}
    payload["merged_review_package"] = build_merged_event_occurrence_package(
        document_id=stage_output.document.doc_id,
        clauses=list(validation.get("source_clauses") or []),
        assignment_package=assignment_package,
        occurrence_payload=payload,
    )
    provenance = deepcopy(stage_output.provenance or {})
    provenance.update({"human_edited_at": timezone.now().isoformat(), "human_edit_source": "live_ui"})
    stage_output.raw_output = text
    stage_output.payload = payload
    stage_output.provenance = provenance
    stage_output.status = StageOutput.Status.CHECKING
    stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])


def save_assembler_output(
    stage_output: StageOutput, raw_output: str, *, occurrence_output: StageOutput,
) -> None:
    text = str(raw_output or "").strip()
    if not text:
        raise LiveWorkflowError("Assembler output cannot be empty")
    validation = validate_occurrence_conservation(occurrence_output.raw_output, text)
    if not validation.get("valid"):
        raise LiveWorkflowError(
            "Edited Assembler output must preserve every accepted E/A/Q line and its order"
        )
    payload = deepcopy(stage_output.payload or {})
    payload["validation"] = validation
    payload["human_edited"] = True
    payload["edited_at"] = timezone.now().isoformat()
    provenance = deepcopy(stage_output.provenance or {})
    provenance.update({"human_edited_at": timezone.now().isoformat(), "human_edit_source": "live_ui"})
    stage_output.raw_output = text
    stage_output.payload = payload
    stage_output.provenance = provenance
    stage_output.status = StageOutput.Status.CHECKING
    stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])


def _persist_simple_result(stage_output: StageOutput, result: Any) -> None:
    attempt = StageExecutionAttempt.objects.create(
        stage_output=stage_output,
        request_id=(result.request or {}).get("request_id") if result.request else None,
        stage=stage_output.stage,
        execution_status=result.status,
        disposition=StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING if result.raw_output else StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED,
        provider=result.provider,
        model=result.model,
        raw_output=result.raw_output,
        payload=result.payload,
        provenance=result.provenance,
        validation=result.validation,
        error=result.error,
        applied_to_stage_output=bool(result.raw_output),
    )
    if result.raw_output:
        stage_output.raw_output = result.raw_output
        stage_output.payload = result.payload
        stage_output.provenance = {**result.provenance, "attempt_id": attempt.pk}
        stage_output.status = StageOutput.Status.CHECKING
        stage_output.save(update_fields=["raw_output", "payload", "provenance", "status", "updated_at"])
    if not result.raw_output:
        raise LiveWorkflowError(result.error or f"{stage_output.stage} did not produce output")


def _source_paragraphs(source_text: str) -> list[str]:
    return [item.strip() for item in re.split(r"\n\s*\n+", str(source_text or "").strip()) if item.strip()]


def _replace_document_paragraph_clauses(document: Any, source_text: str) -> None:
    Clause.objects.filter(document=document).delete()
    Clause.objects.bulk_create([
        Clause(
            document=document,
            clause_id=str(index).zfill(3),
            sequence=index,
            text=paragraph,
        )
        for index, paragraph in enumerate(_source_paragraphs(source_text), start=1)
    ])


def _choose_candidate(client: GpuLocalProviderClient, case: dict[str, Any], candidates: list[dict[str, Any]], k: int) -> dict[str, Any]:
    blocks = "\n\n".join(
        f"[{row['rank']}] {row['event_id']} | {row['headword']}\nDefinition: {row.get('definition','')}\nLLM Example: {row.get('llm_example','')}"
        for row in candidates
    )
    system = "Choose the best controlled Event for the historical EventCut. Select only supplied candidates or none_of_these_fit. Return one JSON object, no markdown."
    user = f"EventCut: {case['event_cut_text']}\n\nCandidates:\n{blocks}\n\nReturn JSON with decision, selected_candidate, confidence_label, reason."
    request_id = f"live-chooser-{uuid4().hex}"
    response = client.generate({
        "schema_version": "emtl-stage-execution-request-v1", "contract_version": "emtl-stage-contract-v1",
        "payload_schema_version": "emtl-provider-api-payload-draft-v1", "request_id": request_id,
        "stage_id": "event_candidate_chooser", "stage_label": "Event Candidate Chooser", "provider": "gpu_local",
        "document_id": str(case.get("event_cut_id") or ""), "document_title": "Live EventCut", "document_type": "eventcut",
        "required_stage_ids": [], "accepted_upstream_stage_ids": [], "correction_requested": False, "inputs": {},
        "prompt_package": {"system_prompt": system, "user_prompt": user, "prompt_character_count": len(system)+len(user), "source_completeness": {"source_complete": True, "truncation_applied": False}},
        "options": {"timeout_seconds": 3600, "max_output_tokens": 256},
    })
    if response.status != ExecutionStatus.COMPLETED.value:
        raise LiveWorkflowError(response.error or "Event chooser failed")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.raw_output.strip(), flags=re.I)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise LiveWorkflowError("Event chooser did not return a JSON decision") from exc
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as nested_exc:
            raise LiveWorkflowError("Event chooser returned malformed JSON") from nested_exc
    decision = _normalize_chooser_decision(data)
    data["decision"] = decision
    if decision not in {"choose_candidate", "none_of_these_fit"}:
        raise LiveWorkflowError("Event chooser returned an invalid decision")
    if decision == "choose_candidate":
        match = _resolve_chooser_candidate(data, candidates)
        if match is None:
            model_selection = deepcopy(data.get("selected_candidate") or data.get("candidate"))
            data["decision"] = "none_of_these_fit"
            data["selected_candidate"] = None
            data["model_selected_candidate"] = model_selection
            data["fallback_reason"] = "chooser_out_of_list_candidate"
            data["reason"] = (
                "The model selection could not be mapped to the supplied top-k candidates; "
                "human review is required. " + str(data.get("reason") or "")
            ).strip()
        else:
            data["selected_candidate"] = {
                "rank": match["rank"],
                "event_id": match["event_id"],
                "headword": match["headword"],
            }
    else:
        data["selected_candidate"] = None
    return data


def _resolve_chooser_candidate(
    data: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    selected = data.get("selected_candidate") or data.get("candidate") or {}
    selected_dict = selected if isinstance(selected, dict) else {}
    selected_text = str(selected if isinstance(selected, (str, int)) else "").strip()
    event_id_signals = [
        selected_dict.get("event_id"), selected_dict.get("id"),
        data.get("selected_event_id"), data.get("event_id"),
    ]
    event_id_signals = [str(value).strip() for value in event_id_signals if value]

    def identifier(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    for signal in event_id_signals:
        normalized = identifier(signal)
        match = next(
            (row for row in candidates if identifier(row.get("event_id")) == normalized), None
        )
        if match is not None:
            return match
    if selected_text:
        normalized_text = identifier(selected_text)
        contained = [
            row for row in candidates
            if len(identifier(row.get("event_id"))) >= 4
            and identifier(row.get("event_id")) in normalized_text
        ]
        if len(contained) == 1:
            return contained[0]

    rank_signal = (
        selected_dict.get("rank") or data.get("selected_rank")
        or data.get("candidate_rank") or data.get("rank")
    )
    if rank_signal is not None:
        rank_match = re.search(r"\d+", str(rank_signal))
        rank = int(rank_match.group(0)) if rank_match else -1
        match = next(
            (row for row in candidates if int(row.get("rank") or -2) == rank), None
        )
        if match is not None:
            return match

    headword_signal = (
        selected_dict.get("headword") or selected_dict.get("name")
        or data.get("selected_headword") or data.get("headword") or selected_text
    )
    normalized_headword = " ".join(str(headword_signal or "").lower().split())
    headword_matches = [
        row for row in candidates
        if " ".join(str(row.get("headword") or "").lower().split()) == normalized_headword
    ]
    return headword_matches[0] if len(headword_matches) == 1 else None


def _normalize_chooser_decision(data: dict[str, Any]) -> str:
    raw = str(data.get("decision") or data.get("choice") or data.get("result") or "")
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    if normalized in {
        "choose_candidate", "selected_candidate", "select_candidate", "candidate",
        "choose", "select", "selected", "candidate_selected", "match", "best_match",
    }:
        return "choose_candidate"
    if normalized in {
        "none_of_these_fit", "none_fit", "none_of_the_above", "none",
        "no_match", "no_suitable_candidate", "none_suitable",
    }:
        return "none_of_these_fit"
    if data.get("selected_candidate") or data.get("candidate") or data.get("selected_rank"):
        return "choose_candidate"
    return normalized


class RemoteQwen3LookupRunner:
    def run(self, eventcut_output: StageOutput) -> dict[str, Any]:
        return self._run_payload(eventcut_output.payload or {})

    def proposal_similarity(self, eventcut_output: StageOutput, *, proposed_headword: str, definition_hint: str) -> dict[str, Any]:
        cut_id = f"proposal-{uuid4().hex}"
        payload = {
            "doc_id": eventcut_output.document.doc_id,
            "source_clause_parser_stage_output_id": (eventcut_output.payload or {}).get("source_clause_parser_stage_output_id"),
            "parsed_event_cuts": [{"valid": True, "event_cut_id": cut_id, "clause_id": "proposal", "event_cut_text": f"{proposed_headword} {definition_hint}".strip()}],
        }
        result = self._run_payload(payload)
        candidates = list((((result.get("cases") or [])[0].get("strategies") or {}).get("hybrid") or {}).get("top_10") or [])
        return {
            "contract_version": "event-headword-proposal-similarity-v1", "status": "completed",
            "query": {"proposed_headword": proposed_headword, "definition_hint": definition_hint, "text": f"{proposed_headword} {definition_hint}".strip()},
            "encoder_model": "Qwen/Qwen3-Embedding-8B", "authority_hash": (result.get("provenance") or {}).get("workbook_sha256", ""),
            "authority_count": (result.get("provenance") or {}).get("authority_row_count", 0), "embedding_dim": (result.get("provenance") or {}).get("embedding_dim", 0),
            "exact_or_morphological_matches": [],
            "matches": [{**row, "cosine_similarity": row.get("score")} for row in candidates],
            "user_override_permitted": True, "official_event_list_modified": False,
        }

    def _run_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if os.getenv("EMTL_LIVE_REMOTE_LOOKUP", "1") != "1":
            raise LiveWorkflowError("Remote Qwen3 lookup is disabled")
        self._validate_configuration()
        job = f"live-{uuid4().hex}"
        remote_dir = f"{REMOTE_BASE}/emtl_live_jobs/{job}"
        self._stop_provider()
        try:
            with tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                eventcuts = local / "eventcuts.json"
                eventcuts.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                self._run(["ssh", "-F", str(SSH_CONFIG), REMOTE_SSH_TARGET, f"mkdir -p {remote_dir}/out"])
                self._run(["scp", "-F", str(SSH_CONFIG), str(QWEN3_TOOL), str(EVENT_WORKBOOK), str(eventcuts), f"{REMOTE_SSH_TARGET}:{remote_dir}/"])
                command = (
                    f"source {REMOTE_VENV}/bin/activate; export HF_HOME={REMOTE_BASE}/emtl_hf_cache; "
                    f"python {remote_dir}/qwen3_event_lookup.py --model-path {REMOTE_MODEL} "
                    f"--workbook {remote_dir}/Events_List_VectorLLM_v1.xlsx --eventcuts {remote_dir}/eventcuts.json "
                    f"--cache-dir {REMOTE_CACHE} --output-dir {remote_dir}/out --device cuda:0 --batch-size 4"
                )
                self._run(["ssh", "-F", str(SSH_CONFIG), REMOTE_SSH_TARGET, command], timeout=7200)
                result_path = local / "real_eventcut_topk.json"
                self._run(["scp", "-F", str(SSH_CONFIG), f"{REMOTE_SSH_TARGET}:{remote_dir}/out/real_eventcut_topk.json", str(result_path)])
                return json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            self._start_provider()

    def _stop_provider(self) -> None:
        command = "pid=$(fuser 8001/tcp 2>/dev/null | awk '{print $1}'); if [ -n \"$pid\" ] && tr '\\0' ' ' </proc/$pid/cmdline | grep -q 'gpu_provider_server.py'; then kill $pid; fi"
        self._run(["ssh", "-F", str(SSH_CONFIG), REMOTE_SSH_TARGET, command])

    def _start_provider(self) -> None:
        command = (
            "ss -ltn 2>/dev/null | grep -q ':8001 ' || "
            f"setsid -f env HF_HOME={REMOTE_BASE}/emtl_hf_cache PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
            f"{REMOTE_VENV}/bin/python {REMOTE_PROVIDER_SCRIPT} --host 127.0.0.1 --port 8001 "
            f"--mode transformers_local --model-path {REMOTE_PROVIDER_MODEL} --model-cache-dir {REMOTE_BASE}/emtl_hf_cache "
            "--max-new-tokens 4096 --device-map balanced_low_0 --device-map-profile qwen2_5_32b_long_context_v2 "
            "--attention-implementation emtl_sdpa_efficient_expanded_gqa --vram-reserve-gib 4 --generation-runtime-margin-mib 4096 "
            f">{REMOTE_PROVIDER_LOG} 2>&1"
        )
        self._run(["ssh", "-F", str(SSH_CONFIG), REMOTE_SSH_TARGET, command])
        endpoint = os.getenv("EMTL_GPU_LOCAL_URL", "http://127.0.0.1:18001/generate").replace("/generate", "/health")
        deadline = time.time() + 900
        while time.time() < deadline:
            try:
                if requests.get(endpoint, timeout=5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(5)
        raise LiveWorkflowError("Qwen provider did not become healthy after dense lookup")

    @staticmethod
    def _validate_configuration() -> None:
        missing = [
            name for name, value in {
                "EMTL_REMOTE_SSH_TARGET": REMOTE_SSH_TARGET,
                "EMTL_REMOTE_MODEL_ROOT": REMOTE_BASE,
                "EMTL_REMOTE_EMBEDDING_MODEL": REMOTE_MODEL,
                "EMTL_REMOTE_PYTHON_ENV": REMOTE_VENV,
                "EMTL_REMOTE_LOOKUP_CACHE": REMOTE_CACHE,
                "EMTL_REMOTE_PROVIDER_SCRIPT": REMOTE_PROVIDER_SCRIPT,
                "EMTL_REMOTE_GENERATION_MODEL": REMOTE_PROVIDER_MODEL,
                "EMTL_REMOTE_PROVIDER_LOG": REMOTE_PROVIDER_LOG,
            }.items() if not str(value).strip()
        ]
        if missing:
            raise LiveWorkflowError(
                "Local remote-model execution is not configured: " + ", ".join(missing)
            )
        for path, label in ((SSH_CONFIG, "SSH config"), (QWEN3_TOOL, "lookup tool"), (EVENT_WORKBOOK, "Event workbook")):
            if not path.exists():
                raise LiveWorkflowError(f"Local {label} is unavailable: {path}")

    @staticmethod
    def _run(command: list[str], timeout: int = 180) -> None:
        attempts = max(1, int(os.getenv("EMTL_REMOTE_COMMAND_ATTEMPTS", "5")))
        retry_markers = (
            "kex_exchange_identification", "connection reset", "connection closed",
            "connection timed out", "operation timed out", "broken pipe",
            "connection refused", "connection aborted",
        )
        last_error = "Remote command failed"
        for attempt in range(1, attempts + 1):
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            if completed.returncode == 0:
                return
            last_error = (completed.stderr or completed.stdout or last_error).strip()
            retryable = any(marker in last_error.lower() for marker in retry_markers)
            if not retryable or attempt == attempts:
                break
            time.sleep(min(2 ** attempt, 15))
        raise LiveWorkflowError(last_error)
