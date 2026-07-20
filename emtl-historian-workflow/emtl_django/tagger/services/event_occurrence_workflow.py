from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from tagger.models import StageOutput

from .event_headword_review import CONTRACT_VERSION as REVIEW_CONTRACT_VERSION
from .eventcut_extraction import INTERNAL_CONTRACT_VERSION


ASSIGNMENT_PACKAGE_CONTRACT = "event-assignment-downstream-v1"
MERGED_PACKAGE_CONTRACT = "event-occurrence-merged-review-v1"


class EventOccurrenceWorkflowError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_accepted_event_assignments(
    *,
    review_store_path: Path,
    eventcut_output: StageOutput,
    clause_ids: list[str],
) -> dict[str, Any]:
    if eventcut_output.stage != StageOutput.Stage.EVENTCUT_EXTRACTION:
        raise EventOccurrenceWorkflowError(
            "eventcut_stage_required", "Event assignment gate requires EventCut StageOutput"
        )
    payload = eventcut_output.payload or {}
    if (
        payload.get("contract_version") != INTERNAL_CONTRACT_VERSION
        or payload.get("internal_usable_for_lookup") is not True
    ):
        raise EventOccurrenceWorkflowError(
            "validated_eventcuts_required", "EventCuts are not validated for lookup"
        )
    store = json.loads(Path(review_store_path).read_text(encoding="utf-8"))
    if store.get("contract_version") != REVIEW_CONTRACT_VERSION:
        raise EventOccurrenceWorkflowError(
            "review_store_contract_invalid", "Event headword review store contract is invalid"
        )
    items = store.get("items")
    if not isinstance(items, dict):
        raise EventOccurrenceWorkflowError(
            "review_store_invalid", "Event headword review store requires an items object"
        )
    selected_clause_ids = {_clause_id(value) for value in clause_ids}
    cuts = [
        deepcopy(cut)
        for cut in payload.get("parsed_event_cuts") or []
        if isinstance(cut, dict)
        and cut.get("valid") is True
        and _clause_id(cut.get("clause_id")) in selected_clause_ids
    ]
    if not cuts:
        raise EventOccurrenceWorkflowError(
            "eventcuts_required", "Selected Clauses have no validated EventCuts"
        )
    item_by_cut: dict[str, dict[str, Any]] = {}
    for item in items.values():
        if not isinstance(item, dict):
            continue
        cut_id = str((item.get("event_cut") or {}).get("event_cut_id") or "")
        if cut_id:
            if cut_id in item_by_cut:
                raise EventOccurrenceWorkflowError(
                    "duplicate_event_review", f"Multiple review items exist for {cut_id}"
                )
            item_by_cut[cut_id] = item
    assignments = []
    missing = []
    for cut in cuts:
        cut_id = str(cut.get("event_cut_id") or "")
        item = item_by_cut.get(cut_id)
        if not item or item.get("state") not in {
            "accepted_existing_headword",
            "provisional_headword_pending_review",
        }:
            missing.append(cut_id)
            continue
        if str(item.get("document_id") or "") != eventcut_output.document.doc_id:
            raise EventOccurrenceWorkflowError(
                "review_document_mismatch", f"Review item for {cut_id} belongs to another document"
            )
        if _clause_id(item.get("clause_id")) != _clause_id(cut.get("clause_id")):
            raise EventOccurrenceWorkflowError(
                "review_clause_mismatch", f"Review item for {cut_id} belongs to another Clause"
            )
        assignment = item.get("assignment")
        if not isinstance(assignment, dict) or assignment.get("status") != "accepted":
            missing.append(cut_id)
            continue
        if str(assignment.get("event_cut_id") or "") != cut_id:
            raise EventOccurrenceWorkflowError(
                "assignment_eventcut_mismatch", f"Assignment does not match {cut_id}"
            )
        assignments.append(
            {
                "assignment_id": str(assignment.get("assignment_id") or ""),
                "event_cut_id": cut_id,
                "clause_id": _clause_id(cut.get("clause_id")),
                "event_cut_text": str(cut.get("event_cut_text") or ""),
                "trigger": str(cut.get("trigger") or ""),
                "event_id": str(assignment.get("event_id") or ""),
                "headword": str(assignment.get("headword") or ""),
                "authority_version": str(assignment.get("authority_version") or ""),
                "accepted_by": str(assignment.get("accepted_by") or ""),
                "accepted_at": str(assignment.get("accepted_at") or ""),
                "selection_source": str(assignment.get("source") or ""),
                "review_item_id": str(item.get("item_id") or ""),
                "review_revision": int(item.get("revision") or 0),
            }
        )
    if missing:
        raise EventOccurrenceWorkflowError(
            "accepted_headwords_incomplete",
            "Every EventCut must have a human-accepted existing Event headword before "
            "Occurrence generation. Missing: " + ", ".join(missing),
        )
    return {
        "contract_version": ASSIGNMENT_PACKAGE_CONTRACT,
        "document_id": eventcut_output.document.doc_id,
        "source_eventcut_stage_output_id": eventcut_output.pk,
        "selected_clause_ids": sorted(selected_clause_ids),
        "eventcut_count": len(cuts),
        "assignment_count": len(assignments),
        "headwords_complete_for_selected_clauses": len(assignments) == len(cuts),
        "assignments": assignments,
    }


def build_merged_event_occurrence_package(
    *,
    document_id: str,
    clauses: list[dict[str, Any]],
    assignment_package: dict[str, Any],
    occurrence_payload: dict[str, Any],
) -> dict[str, Any]:
    if assignment_package.get("contract_version") != ASSIGNMENT_PACKAGE_CONTRACT:
        raise EventOccurrenceWorkflowError(
            "assignment_contract_invalid", "Event assignment package contract is invalid"
        )
    occurrence_validation = occurrence_payload.get("validation") or {}
    parsed_by_clause = {
        _clause_id(row.get("clause_id")): row
        for row in occurrence_validation.get("parsed_clauses") or []
        if isinstance(row, dict)
    }
    assignments_by_clause: dict[str, list[dict[str, Any]]] = {}
    for assignment in assignment_package.get("assignments") or []:
        assignments_by_clause.setdefault(_clause_id(assignment.get("clause_id")), []).append(
            assignment
        )
    merged_clauses = []
    for clause in clauses:
        clause_id = _clause_id(clause.get("clause_id"))
        parsed = parsed_by_clause.get(clause_id, {})
        tags = list(parsed.get("tags") or [])
        event_tags = {
            str(tag.get("event_cut_id") or ""): tag
            for tag in tags
            if tag.get("type") == "E" and tag.get("event_cut_id")
        }
        events = []
        for assignment in assignments_by_clause.get(clause_id, []):
            cut_id = assignment["event_cut_id"]
            tag = deepcopy(event_tags.get(cut_id))
            events.append(
                {
                    "event_cut": {
                        "event_cut_id": cut_id,
                        "text": assignment["event_cut_text"],
                        "trigger": assignment["trigger"],
                    },
                    "accepted_headword": {
                        "assignment_id": assignment["assignment_id"],
                        "event_id": assignment["event_id"],
                        "headword": assignment["headword"],
                        "accepted_by": assignment["accepted_by"],
                        "accepted_at": assignment["accepted_at"],
                    },
                    "occurrence": (
                        {
                            "occurrence_id": _stable_id(
                                document_id, clause_id, cut_id, assignment["event_id"]
                            ),
                            "review_state": "checking",
                            "tag": tag,
                        }
                        if tag
                        else None
                    ),
                }
            )
        merged_clauses.append(
            {
                "clause_id": clause_id,
                "clause_text": str(clause.get("text") or ""),
                "events": events,
                "attribute_tags": [tag for tag in tags if tag.get("type") == "A"],
                "quantified_statement_tags": [tag for tag in tags if tag.get("type") == "Q"],
                "unresolved_event_suggestions": deepcopy(
                    parsed.get("unresolved_event_suggestions") or []
                ),
            }
        )
    return {
        "contract_version": MERGED_PACKAGE_CONTRACT,
        "document_id": document_id,
        "organization_unit": "clause",
        "event_unit": "eventcut",
        "review_state": "checking",
        "headword_selection_is_upstream_authoritative": True,
        "occurrence_may_not_reselect_headword": True,
        "clauses": merged_clauses,
    }


def _clause_id(value: Any) -> str:
    return str(value or "").strip().zfill(3)


def _stable_id(*parts: str) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return "event-occurrence-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
