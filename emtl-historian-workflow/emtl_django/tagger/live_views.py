from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from .models import Document, NewIdProposal, StageOutput
from .services.entity_review_handoff import ENTITY_RECORD_TYPES, propose_entity
from .services.export_contract import build_workflow_export
from .services.live_workflow import (
    LiveWorkflowError,
    accept_remaining_entities,
    accept_remaining_headwords,
    accept_stage,
    apply_headword_action,
    finalize_entity_review,
    run_assembler,
    run_clause,
    run_dense_and_chooser,
    run_entity,
    run_eventcuts,
    run_occurrence,
    run_summary,
    save_entity_review_decision,
    save_assembler_output,
    save_clause_output,
    save_occurrence_output,
    save_source_document,
    save_summary_output,
    sync_eventcut_review_status,
)
from .services.workflow_orchestrator import build_orchestration_plan
from .services.providers.factory import ProviderIntegrationRequired, stage_generation_client
from .stage_config import STAGE_PROFILES, dependency_closure, ordered_stage_ids


LIVE_STAGES = {
    "summary_keywords", "entity_registry", "clause_parser",
    "occurrences_registry", "tag_assembler",
}


def live_workbench(request: HttpRequest) -> HttpResponse:
    document = _active_live_document(request)
    _ensure_outputs(document)
    if request.method == "POST":
        return _handle_action(request, document)
    outputs = {row.stage: row for row in document.stage_outputs.all()}
    requested = set(request.session.get("live_selected_stages") or LIVE_STAGES)
    requested &= LIVE_STAGES
    expanded = dependency_closure(requested)
    plan = build_orchestration_plan(
        document=document, requested_stages=requested, stage_outputs=outputs
    )
    entity_output = outputs.get("entity_registry")
    entity_rows = []
    if entity_output:
        decisions = dict((entity_output.payload or {}).get("live_entity_decisions") or {})
        for index, row in enumerate((((entity_output.payload or {}).get("entity_output") or {}).get("tags") or [])):
            decision = decisions.get(str(index), {})
            reviewed_row = decision.get("edited_row") if decision.get("decision") == "edited" else None
            entity_rows.append({
                "index": index,
                "row": reviewed_row or row,
                "original_row": row,
                "decision": decision,
                "status": str(decision.get("decision") or "pending"),
            })
    eventcut = outputs.get(StageOutput.Stage.EVENTCUT_EXTRACTION)
    if eventcut and ((eventcut.payload or {}).get("headword_review_store") or {}).get("items"):
        sync_eventcut_review_status(eventcut)
    review_items = _friendly_headword_items(
        list((((eventcut.payload if eventcut else {}) or {}).get("headword_review_store") or {}).get("items", {}).values())
    )
    provider_health = _provider_health()
    plan_dict = _friendly_plan(plan.as_dict())
    summary_ready = "summary_keywords" not in expanded or outputs["summary_keywords"].status == StageOutput.Status.ACCEPTED
    clause_ready = "clause_parser" not in expanded or outputs["clause_parser"].status == StageOutput.Status.ACCEPTED
    entity_ready = "entity_registry" not in expanded or outputs["entity_registry"].status == StageOutput.Status.ACCEPTED
    terminal_headword_states = {"accepted_existing_headword", "provisional_headword_pending_review"}
    headword_review_complete = bool(review_items) and all(
        str(item.get("state") or "") in terminal_headword_states for item in review_items
    )
    context = {
        "document": document,
        "source_text": str((document.metadata or {}).get("working_source_text") or ""),
        "outputs": outputs,
        "ordered_stages": [
            {"id": stage_id, "label": _stage_label(stage_id), "profile": STAGE_PROFILES[stage_id], "output": outputs.get(stage_id), "selected": stage_id in requested}
            for stage_id in ordered_stage_ids(expanded)
        ],
        "stage_choices": [{"stage_id": item, "label": _stage_label(item)} for item in ordered_stage_ids(LIVE_STAGES)],
        "requested_stages": requested,
        "plan": plan_dict,
        "auto_included_labels": [
            _stage_label(item) for item in plan.auto_included_stages if item in STAGE_PROFILES
        ],
        "entity_rows": entity_rows,
        "entity_proposals": list(
            document.new_id_proposals.select_related("source_clause").filter(
                status=NewIdProposal.Status.APPROVED
            )
        ),
        "entity_record_types": sorted(ENTITY_RECORD_TYPES),
        "eventcut_output": eventcut,
        "event_cuts": list(((eventcut.payload if eventcut else {}) or {}).get("parsed_event_cuts") or []),
        "headword_items": review_items,
        "headword_pending_accept_count": sum(1 for item in review_items if item.get("state") == "llm_selected_candidate"),
        "summary_ready": summary_ready,
        "clause_ready": clause_ready,
        "entity_ready": entity_ready,
        "headword_review_complete": headword_review_complete,
        "notice": request.session.pop("live_notice", ""),
        "error": request.session.pop("live_error", ""),
        "provider_health": provider_health,
        "export_ready": bool(outputs),
    }
    return render(request, "tagger/live_workbench.html", context)


def live_export(request: HttpRequest) -> HttpResponse:
    from django.http import JsonResponse
    document = _active_live_document(request)
    package = build_workflow_export(document)
    response = JsonResponse(package, json_dumps_params={"indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{package["export_id"]}.json"'
    return response


def _handle_action(request: HttpRequest, document: Document) -> HttpResponse:
    action = str(request.POST.get("action") or "")
    outputs = {row.stage: row for row in document.stage_outputs.all()}
    try:
        if action == "configure":
            selected = set(request.POST.getlist("stages")) & LIVE_STAGES
            request.session["live_selected_stages"] = sorted(selected or LIVE_STAGES)
            request.session["live_notice"] = "Workflow selection updated. Required earlier bots are added automatically when a later bot depends on them."
        elif action == "save_source":
            save_source_document(document, str(request.POST.get("source_text") or ""))
            request.session["live_notice"] = "Source document saved. Existing bot outputs were reset so downstream results use the revised text."
        elif action == "save_stage_text":
            stage_id = str(request.POST.get("stage_id") or "")
            if stage_id == StageOutput.Stage.SUMMARY_KEYWORDS:
                save_summary_output(outputs[stage_id], str(request.POST.get("raw_output") or ""))
                request.session["live_notice"] = "Summary & Keywords edit saved."
            elif stage_id == StageOutput.Stage.CLAUSE_PARSER:
                save_clause_output(outputs[stage_id], str(request.POST.get("raw_output") or ""))
                request.session["live_notice"] = "Clause Parser edit saved and coverage was revalidated."
            elif stage_id == StageOutput.Stage.OCCURRENCES_REGISTRY:
                save_occurrence_output(
                    outputs[stage_id], str(request.POST.get("raw_output") or ""),
                    clause_output=outputs[StageOutput.Stage.CLAUSE_PARSER],
                    entity_output=outputs[StageOutput.Stage.ENTITY_REGISTRY],
                )
                request.session["live_notice"] = "Occurrence Registry edit saved and revalidated."
            elif stage_id == StageOutput.Stage.TAG_ASSEMBLER:
                save_assembler_output(
                    outputs[stage_id], str(request.POST.get("raw_output") or ""),
                    occurrence_output=outputs[StageOutput.Stage.OCCURRENCES_REGISTRY],
                )
                request.session["live_notice"] = "Assembler edit saved and E/A/Q conservation was revalidated."
            else:
                raise LiveWorkflowError("This stage cannot be edited from the live text editor.")
        elif action == "run_summary":
            run_summary(outputs["summary_keywords"])
            request.session["live_notice"] = "Summary & Keywords generated and ready for review."
        elif action == "run_clause":
            run_clause(outputs["clause_parser"])
            request.session["live_notice"] = "Clause Parser completed and passed source coverage validation."
        elif action == "run_entity":
            run_entity(outputs["entity_registry"])
            request.session["live_notice"] = "Entity Registry generated and is ready for row review."
        elif action == "accept_stage":
            accept_stage(outputs[str(request.POST["stage_id"])])
            request.session["live_notice"] = "Stage accepted."
        elif action == "entity_decision":
            index = int(request.POST["row_index"])
            decision = str(request.POST["decision"])
            edited = None
            if decision == "edited":
                original = list((((outputs["entity_registry"].payload or {}).get("entity_output") or {}).get("tags") or []))[index]
                edited = {**deepcopy(original),
                    "type": str(request.POST.get("record_type") or original.get("type") or ""),
                    "id": str(request.POST.get("stable_id") or original.get("id") or ""),
                    "headword": str(request.POST.get("headword") or original.get("headword") or ""),
                }
            save_entity_review_decision(outputs["entity_registry"], row_index=index, decision=decision, edited_row=edited)
            request.session["live_notice"] = "Entity review decision saved."
        elif action == "propose_entity":
            proposal = propose_entity(
                stage_output=outputs["entity_registry"],
                record_type=str(request.POST.get("record_type") or ""),
                headword=str(request.POST.get("headword") or ""),
                evidence_form=str(request.POST.get("evidence_form") or ""),
            )
            proposal.status = NewIdProposal.Status.APPROVED
            proposal.save(update_fields=["status", "updated_at"])
            request.session["live_notice"] = "Your Entity was added and accepted."
        elif action == "accept_remaining_entities":
            count = accept_remaining_entities(outputs["entity_registry"])
            proposal_count = document.new_id_proposals.filter(status=NewIdProposal.Status.PENDING).update(status=NewIdProposal.Status.APPROVED)
            request.session["live_notice"] = f"Accepted {count} remaining Entity rows and {proposal_count} pending proposals; edits and rejects were preserved."
        elif action == "finalize_entity":
            if document.new_id_proposals.filter(status=NewIdProposal.Status.PENDING).exists():
                raise LiveWorkflowError("Resolve or bulk-accept pending Entity proposals first")
            finalize_entity_review(outputs["entity_registry"])
            request.session["live_notice"] = "Entity review finalized for downstream use."
        elif action == "proposal_decision":
            proposal = document.new_id_proposals.get(pk=int(request.POST["proposal_id"]))
            decision = str(request.POST["decision"])
            if decision not in {"approved", "rejected"}:
                raise LiveWorkflowError("Invalid Entity proposal decision")
            proposal.status = decision
            proposal.save(update_fields=["status", "updated_at"])
            request.session["live_notice"] = "Entity proposal decision saved."
        elif action == "run_eventcuts":
            _require_accepted(outputs, "clause_parser", "entity_registry")
            run_eventcuts(outputs["clause_parser"])
            request.session["live_notice"] = "EventCuts extracted and validated."
        elif action == "run_lookup":
            eventcut = outputs[StageOutput.Stage.EVENTCUT_EXTRACTION]
            run_dense_and_chooser(eventcut, top_k=20)
            request.session["live_notice"] = "Event headword matching completed."
        elif action == "headword_action":
            eventcut = outputs[StageOutput.Stage.EVENTCUT_EXTRACTION]
            updated = apply_headword_action(
                eventcut,
                item_id=str(request.POST["item_id"]),
                action=str(request.POST["review_action"]),
                actor="local_historian",
                candidate_rank=int(request.POST["candidate_rank"]) if request.POST.get("candidate_rank") else None,
                proposed_headword=str(request.POST.get("proposed_headword") or ""),
                definition_hint=str(request.POST.get("definition_hint") or ""),
                reviewer_note=str(request.POST.get("reviewer_note") or ""),
            )
            request.session["live_notice"] = f"Headword review saved: {updated['state']}."
        elif action == "accept_remaining_headwords":
            count = accept_remaining_headwords(outputs[StageOutput.Stage.EVENTCUT_EXTRACTION], actor="local_historian")
            request.session["live_notice"] = f"Accepted {count} remaining LLM headword choices."
        elif action == "run_occurrence":
            _require_accepted(outputs, "clause_parser", "entity_registry")
            run_occurrence(
                occurrence_output=outputs["occurrences_registry"],
                clause_output=outputs["clause_parser"],
                entity_output=outputs["entity_registry"],
                eventcut_output=outputs[StageOutput.Stage.EVENTCUT_EXTRACTION],
            )
            request.session["live_notice"] = "Occurrence Registry completed and is ready for review."
        elif action == "run_assembler":
            _require_accepted(outputs, "clause_parser", "entity_registry", "occurrences_registry")
            run_assembler(
                assembler_output=outputs["tag_assembler"],
                clause_output=outputs["clause_parser"],
                entity_output=outputs["entity_registry"],
                occurrence_output=outputs["occurrences_registry"],
            )
            request.session["live_notice"] = "Assembler completed; E/A/Q conservation was checked."
        else:
            raise LiveWorkflowError("Unknown live workflow action")
    except Exception as exc:
        request.session["live_error"] = f"{type(exc).__name__}: {exc}"
    return redirect(reverse("tagger:live_workbench"))


def _active_live_document(request: HttpRequest) -> Document:
    pk = request.session.get("active_document_pk")
    document = Document.objects.filter(pk=pk).first() if pk else None
    if document is None or (document.metadata or {}).get("workflow_source") != "uploaded_document":
        raise Http404("Upload a document to use the live workflow.")
    return document


def _ensure_outputs(document: Document) -> None:
    for stage_id, profile in STAGE_PROFILES.items():
        StageOutput.objects.get_or_create(
            document=document, stage=stage_id,
            defaults={"display_title": profile.label, "status": "blocked" if profile.future else "not_started"},
        )


def _require_accepted(outputs: dict[str, StageOutput], *stage_ids: str) -> None:
    missing = [stage_id for stage_id in stage_ids if outputs.get(stage_id) is None or outputs[stage_id].status != StageOutput.Status.ACCEPTED]
    if missing:
        raise LiveWorkflowError("Accept required stages first: " + ", ".join(missing))


def _provider_health() -> dict[str, Any]:
    try:
        client = stage_generation_client()
        health_method = getattr(client, "health", None)
        if health_method is None:
            return {
                "ok": True,
                "provider": getattr(client, "provider_name", "configured"),
                "model": "",
            }
        health = health_method()
        payload = dict(getattr(health, "payload", {}) or {})
        return {
            "ok": bool(getattr(health, "ok", False)),
            "provider": getattr(client, "provider_name", payload.get("provider", "")),
            "model": payload.get("model", ""),
            "error": str(getattr(health, "error", "") or ""),
        }
    except ProviderIntegrationRequired as exc:
        return {
            "ok": False,
            "provider": os.getenv("EMTL_STAGE_PROVIDER", "unconfigured"),
            "model": "",
            "error": str(exc),
        }
    except Exception as exc:
        return {"ok": False, "provider": "", "model": "", "error": str(exc)}


def _friendly_plan(plan: dict[str, Any]) -> dict[str, Any]:
    plan = dict(plan)
    plan["steps"] = [
        {
            **step,
            "label": _stage_label(str(step.get("stage_id") or "")),
            "friendly_status": _friendly_status(str(step.get("stage_status") or "")),
            "friendly_action": _friendly_action(str(step.get("action") or "")),
        }
        for step in list(plan.get("steps") or [])
    ]
    return plan


def _stage_label(stage_id: str) -> str:
    return {
        "summary_keywords": "Summary & Keywords",
        "entity_registry": "Entity Registry",
        "clause_parser": "Clause Parser",
        "occurrences_registry": "Occurrence Registry",
        "tag_assembler": "Assembler",
        "key_narrative": "Key Narrative",
    }.get(stage_id, STAGE_PROFILES[stage_id].label if stage_id in STAGE_PROFILES else stage_id.replace("_", " ").title())


def _friendly_status(status: str) -> str:
    return {
        "not_started": "Not started",
        "loaded": "Loaded",
        "checking": "Needs review",
        "accepted": "Accepted",
        "needs_rerun": "Needs rerun",
        "blocked": "Waiting",
    }.get(status, status.replace("_", " ").title())


def _friendly_action(action: str) -> str:
    return {
        "run": "Ready to run",
        "reuse_accepted": "Ready",
        "await_human_review": "Needs your review",
        "wait_for_dependencies": "Waiting for earlier steps",
        "retry_after_dependency_recheck": "Waiting for earlier steps",
        "rerun": "Ready to rerun",
        "complete": "Complete",
    }.get(action, action.replace("_", " ").title())


def _friendly_headword_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {
        "llm_selected_candidate": "Pending review",
        "llm_none_fit": "Needs a new headword",
        "editing_headword": "Editing",
        "proposal_similarity_review": "Compare similar headwords",
        "accepted_existing_headword": "Accepted",
        "provisional_headword_pending_review": "Accepted proposal",
    }
    status_classes = {
        "accepted_existing_headword": "accepted",
        "provisional_headword_pending_review": "accepted",
        "editing_headword": "edited",
        "proposal_similarity_review": "edited",
        "llm_none_fit": "rejected",
    }
    enriched = []
    for source in items:
        item = deepcopy(source)
        state = str(item.get("state") or "")
        item["friendly_state"] = labels.get(state, state.replace("_", " ").title())
        item["status_class"] = status_classes.get(state, "pending")
        item["final_assignment"] = deepcopy(item.get("assignment") or {})
        item["similarity_matches"] = list((item.get("similarity_check") or {}).get("matches") or [])
        enriched.append(item)
    return enriched
