from __future__ import annotations

from typing import Any

from tagger.models import NewIdProposal, ReviewNote, StageOutput


RESERVED_DECISION_TYPES = ("resolve_warning",)


def build_review_decisions_for_export(
    *,
    document_id: str,
    stage_outputs: dict[str, StageOutput],
    stage_notes: list[ReviewNote],
    proposals: list[NewIdProposal],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    decisions.extend(_stage_accept_decisions(document_id, stage_outputs))
    decisions.extend(_stage_rerun_decisions(document_id, stage_notes))
    decisions.extend(_new_id_decisions(document_id, proposals))
    decisions.extend(_entity_row_decisions(document_id, stage_outputs))
    decisions.extend(_event_headword_decisions(document_id, stage_outputs))
    return decisions


def _stage_accept_decisions(
    document_id: str,
    stage_outputs: dict[str, StageOutput],
) -> list[dict[str, Any]]:
    decisions = []
    for stage_id, stage_output in stage_outputs.items():
        if stage_output.status != StageOutput.Status.ACCEPTED:
            continue
        provenance = dict(stage_output.provenance or {})
        decisions.append(
            _decision(
                decision_id=f"stage-output:{document_id}:{stage_id}:accept",
                decision_type="accept",
                target_type="stage_output",
                target_id=f"{document_id}:{stage_id}",
                stage_id=stage_id,
                document_id=document_id,
                source_status=stage_output.status,
                original_value={
                    "status": stage_output.status,
                    "display_title": stage_output.display_title,
                },
                created_at=str(provenance.get("continued_at") or _iso(stage_output.updated_at)),
                is_final=True,
                provenance={
                    "source": "StageOutput.status",
                    "provider": provenance.get("provider", ""),
                    "provider_execution_status": provenance.get("execution_status", ""),
                },
            )
        )
    return decisions


def _stage_rerun_decisions(
    document_id: str,
    stage_notes: list[ReviewNote],
) -> list[dict[str, Any]]:
    decisions = []
    for note in stage_notes:
        if note.requested_action != "chatbot_rerun_request":
            continue
        stage_id = note.stage_output.stage if note.stage_output else ""
        target_id = f"{document_id}:{stage_id}" if stage_id else document_id
        decisions.append(
            _decision(
                decision_id=f"review-note:{note.pk or 'unsaved'}",
                decision_type="request_rerun",
                target_type="stage_output",
                target_id=target_id,
                stage_id=stage_id,
                document_id=document_id,
                source_decision=note.requested_action,
                original_value={
                    "clause_id": note.clause.clause_id if note.clause else "",
                },
                reviewer_note=note.note,
                created_at=_iso(note.created_at),
                created_by=note.created_by_label or "local_user",
                is_final=False,
                provenance={"source": "ReviewNote"},
            )
        )
    return decisions


def _new_id_decisions(
    document_id: str,
    proposals: list[NewIdProposal],
) -> list[dict[str, Any]]:
    status_map = {
        NewIdProposal.Status.APPROVED: ("accept", True),
        NewIdProposal.Status.REJECTED: ("reject", True),
        NewIdProposal.Status.NEEDS_EDIT: ("edit", False),
    }
    decisions = []
    for proposal in proposals:
        mapped = status_map.get(proposal.status)
        if not mapped:
            continue
        decision_type, is_final = mapped
        value = _proposal_value(proposal)
        decisions.append(
            _decision(
                decision_id=f"new-id:{proposal.pk or proposal.proposed_id}:{proposal.status}",
                decision_type=decision_type,
                target_type="new_id",
                target_id=proposal.proposed_id,
                stage_id="entity_registry",
                document_id=document_id,
                source_status=proposal.status,
                original_value=value,
                edited_value=value if proposal.status == NewIdProposal.Status.NEEDS_EDIT else {},
                reviewer_note=proposal.reviewer_note,
                created_at=_iso(proposal.updated_at),
                is_final=is_final,
                provenance={"source": "NewIdProposal.status"},
            )
        )
    return decisions


def _event_headword_decisions(
    document_id: str,
    stage_outputs: dict[str, StageOutput],
) -> list[dict[str, Any]]:
    eventcut_output = stage_outputs.get("eventcut_extraction")
    eventcut_payload = (eventcut_output.payload if eventcut_output else {}) or {}
    current_items = ((eventcut_payload.get("headword_review_store") or {}).get("items") or {})
    if isinstance(current_items, dict) and current_items:
        decisions = []
        for item in current_items.values():
            if not isinstance(item, dict):
                continue
            assignment = item.get("assignment") or {}
            state = str(item.get("state") or "")
            terminal = state in {
                "accepted_existing_headword", "provisional_headword_pending_review",
            }
            proposal = item.get("proposal") or {}
            decisions.append(
                _decision(
                    decision_id=f"event-headword:{item.get('item_id', '')}:{item.get('revision', 0)}",
                    decision_type=(
                        "propose_new" if state == "provisional_headword_pending_review"
                        else "accept_candidate" if state == "accepted_existing_headword"
                        else "review_in_progress"
                    ),
                    target_type="event_headword_assignment",
                    target_id=str((item.get("event_cut") or {}).get("event_cut_id") or ""),
                    stage_id="eventcut_extraction",
                    document_id=document_id,
                    source_status=state,
                    source_decision=str(((item.get("audit_log") or [{}])[-1]).get("action") or ""),
                    original_value={
                        "event_cut": item.get("event_cut") or {},
                        "suggested_candidate": item.get("chooser_selected_candidate") or {},
                    },
                    edited_value=proposal if isinstance(proposal, dict) else {},
                    selected_candidate=assignment if isinstance(assignment, dict) else {},
                    proposed_value=proposal if isinstance(proposal, dict) else {},
                    created_at=str(assignment.get("accepted_at") or item.get("updated_at") or ""),
                    created_by=str(assignment.get("accepted_by") or "local_historian"),
                    is_final=terminal,
                    provenance={
                        "source": "StageOutput.payload.headword_review_store",
                        "review_revision": int(item.get("revision") or 0),
                        "authority_version": str(item.get("authority_version") or ""),
                    },
                )
            )
        return decisions

    stage_output = stage_outputs.get("occurrences_registry")
    payload = (stage_output.payload if stage_output else {}) or {}
    review_payload = payload.get("event_headword_review") or {}
    if not isinstance(review_payload, dict):
        return []
    decisions = []
    for item in review_payload.get("event_review_items") or []:
        if not isinstance(item, dict):
            continue
        review_decision = item.get("review_decision")
        if not isinstance(review_decision, dict):
            continue
        normalized = _normalize_event_headword_decision(
            document_id=document_id,
            item=item,
            review_decision=review_decision,
        )
        if normalized:
            decisions.append(normalized)
    return decisions


def _entity_row_decisions(
    document_id: str, stage_outputs: dict[str, StageOutput]
) -> list[dict[str, Any]]:
    stage_output = stage_outputs.get("entity_registry")
    review = (((stage_output.payload if stage_output else {}) or {}).get("entity_review") or {})
    decisions = []
    for row in review.get("review_rows") or []:
        if not isinstance(row, dict):
            continue
        decision = str(row.get("decision") or "")
        decisions.append(
            _decision(
                decision_id=f"entity-row:{document_id}:{row.get('review_row_id', '')}",
                decision_type=decision,
                target_type="entity_row",
                target_id=str(row.get("review_row_id") or ""),
                stage_id="entity_registry",
                document_id=document_id,
                source_status=str(review.get("state") or ""),
                source_decision=decision,
                original_value=row.get("original_row") if isinstance(row.get("original_row"), dict) else {},
                edited_value=(
                    row.get("reviewed_row")
                    if decision == "edited" and isinstance(row.get("reviewed_row"), dict)
                    else {}
                ),
                selected_candidate=(
                    row.get("reviewed_row")
                    if decision != "rejected" and isinstance(row.get("reviewed_row"), dict)
                    else {}
                ),
                created_at=str(review.get("reviewed_at") or ""),
                created_by="local_historian",
                is_final=True,
                provenance={
                    "source": "StageOutput.payload.entity_review.review_rows",
                    "review_contract": str(review.get("contract_version") or ""),
                },
            )
        )
    return decisions


def _normalize_event_headword_decision(
    *,
    document_id: str,
    item: dict[str, Any],
    review_decision: dict[str, Any],
) -> dict[str, Any] | None:
    review_state = str(item.get("review_state") or review_decision.get("review_state") or "")
    action = str(review_decision.get("action") or "")
    item_id = str(item.get("item_id") or "")
    event_cut = item.get("event_cut") if isinstance(item.get("event_cut"), dict) else {}
    clause = item.get("clause") if isinstance(item.get("clause"), dict) else {}
    original_value = {
        "item_id": item_id,
        "event_cut_id": str(event_cut.get("event_cut_id") or ""),
        "clause_id": str(clause.get("clause_id") or ""),
        "review_state": review_state,
    }
    common = {
        "decision_id": str(review_decision.get("decision_id") or f"event-headword:{item_id}:{review_state}"),
        "stage_id": "occurrences_registry",
        "document_id": document_id,
        "source_status": review_state,
        "source_decision": action,
        "original_value": original_value,
        "reviewer_note": str(review_decision.get("reviewer_note") or ""),
        "created_at": str(review_decision.get("created_at") or ""),
        "created_by": str(review_decision.get("actor") or "local_user"),
        "provenance": {
            "source": "StageOutput.payload.event_headword_review",
            "not_written_to_official_event_list": bool(
                review_decision.get("not_written_to_official_event_list", True)
            ),
        },
    }
    if review_state in {"accepted_llm_choice", "accepted_alternate_candidate"}:
        selected = review_decision.get("selected_candidate")
        return _decision(
            decision_type="accept_candidate",
            target_type="event_candidate",
            target_id=f"{item_id}:{(selected or {}).get('event_id', '')}",
            selected_candidate=selected if isinstance(selected, dict) else {},
            is_final=True,
            **common,
        )
    if review_state == "rejected_all_candidates":
        return _decision(
            decision_type="reject_candidates",
            target_type="event_candidate",
            target_id=item_id,
            is_final=True,
            provenance={
                **common["provenance"],
                "rejected_candidate_scope": review_decision.get("rejected_candidate_scope", ""),
                "requires_followup": bool(review_decision.get("requires_followup", False)),
            },
            **{key: value for key, value in common.items() if key != "provenance"},
        )
    if review_state == "held_for_later":
        return _decision(
            decision_type="hold",
            target_type="event_headword",
            target_id=item_id,
            is_final=False,
            **common,
        )
    if review_state == "proposed_new_headword_pending_review":
        proposed = review_decision.get("proposed_headword")
        return _decision(
            decision_type="propose_new",
            target_type="event_headword",
            target_id=item_id,
            proposed_value=proposed if isinstance(proposed, dict) else {},
            is_final=False,
            provenance={
                **common["provenance"],
                "requires_similarity_check": bool(review_decision.get("requires_similarity_check", False)),
                "requires_historian_review": bool(review_decision.get("requires_historian_review", False)),
            },
            **{key: value for key, value in common.items() if key != "provenance"},
        )
    return None


def _proposal_value(proposal: NewIdProposal) -> dict[str, Any]:
    return {
        "proposed_id": proposal.proposed_id,
        "type": proposal.record_type,
        "headword": proposal.headword,
        "evidence_form": proposal.evidence_form,
        "source_clause": proposal.source_clause.clause_id if proposal.source_clause else proposal.payload.get("source_clause_label", ""),
    }


def _decision(
    *,
    decision_id: str,
    decision_type: str,
    target_type: str,
    target_id: str,
    stage_id: str,
    document_id: str,
    source_status: str = "",
    source_decision: str = "",
    original_value: dict[str, Any] | None = None,
    edited_value: dict[str, Any] | None = None,
    selected_candidate: dict[str, Any] | None = None,
    proposed_value: dict[str, Any] | None = None,
    reviewer_note: str = "",
    created_at: str = "",
    created_by: str = "local_user",
    is_final: bool = True,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "decision_type": decision_type,
        "target_type": target_type,
        "target_id": target_id,
        "stage_id": stage_id,
        "document_id": document_id,
        "source_status": source_status,
        "source_decision": source_decision,
        "original_value": original_value or {},
        "edited_value": edited_value or {},
        "selected_candidate": selected_candidate or {},
        "proposed_value": proposed_value or {},
        "reviewer_note": reviewer_note,
        "created_at": created_at,
        "created_by": created_by,
        "is_final": is_final,
        "provenance": provenance or {},
    }


def _iso(value: Any) -> str:
    return value.isoformat() if value else ""
