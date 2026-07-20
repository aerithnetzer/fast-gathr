from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.test import TestCase

from tagger.models import Document, StageOutput
from tagger.services.event_occurrence_workflow import (
    EventOccurrenceWorkflowError,
    build_merged_event_occurrence_package,
    load_accepted_event_assignments,
)


class EventOccurrenceWorkflowTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(doc_id="doc-1", title="Doc")
        self.eventcut = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION,
            status=StageOutput.Status.CHECKING,
            display_title="EventCut",
            payload={
                "contract_version": "eventcut-extraction-internal-v1",
                "internal_usable_for_lookup": True,
                "parsed_event_cuts": [
                    {
                        "event_cut_id": "cut-1",
                        "clause_id": "001",
                        "event_cut_text": "gave money",
                        "trigger": "gave",
                        "valid": True,
                    }
                ],
            },
        )
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Path(self.directory.name) / "review.json"

    def write_store(self, *, accepted: bool) -> None:
        item = {
            "item_id": "item-1",
            "document_id": "doc-1",
            "clause_id": "001",
            "event_cut": {"event_cut_id": "cut-1", "text": "gave money"},
            "state": "accepted_existing_headword" if accepted else "editing_headword",
            "revision": 1,
            "assignment": (
                {
                    "assignment_id": "assignment-1",
                    "event_cut_id": "cut-1",
                    "event_id": "E-0001",
                    "headword": "Give",
                    "authority_version": "v1",
                    "status": "accepted",
                    "source": "llm_accept",
                    "accepted_by": "historian-1",
                    "accepted_at": "2026-07-01T00:00:00Z",
                }
                if accepted
                else None
            ),
        }
        self.store.write_text(
            json.dumps(
                {
                    "contract_version": "event-headword-human-review-v1",
                    "items": {"item-1": item},
                }
            ),
            encoding="utf-8",
        )

    def test_gate_requires_every_eventcut_to_be_human_accepted(self) -> None:
        self.write_store(accepted=False)
        with self.assertRaisesRegex(EventOccurrenceWorkflowError, "Every EventCut"):
            load_accepted_event_assignments(
                review_store_path=self.store,
                eventcut_output=self.eventcut,
                clause_ids=["001"],
            )

    def test_assignment_package_and_clause_merge(self) -> None:
        self.write_store(accepted=True)
        assignments = load_accepted_event_assignments(
            review_store_path=self.store,
            eventcut_output=self.eventcut,
            clause_ids=["001"],
        )
        occurrence = {
            "validation": {
                "parsed_clauses": [
                    {
                        "clause_id": "001",
                        "tags": [
                            {
                                "type": "E",
                                "event_cut_id": "cut-1",
                                "id": "E-0001",
                                "headword": "Give",
                                "fields": {"Trigger": "gave", "Actor": "John [P-1]"},
                            },
                            {"type": "Q", "id": "Q-0001", "headword": "Monetary Amount"},
                        ],
                        "unresolved_event_suggestions": [
                            {"id": "E-0002", "headword": "Pay"}
                        ],
                    }
                ]
            }
        }
        merged = build_merged_event_occurrence_package(
            document_id="doc-1",
            clauses=[{"clause_id": "001", "text": "he gave money"}],
            assignment_package=assignments,
            occurrence_payload=occurrence,
        )
        clause = merged["clauses"][0]
        self.assertEqual(clause["events"][0]["event_cut"]["event_cut_id"], "cut-1")
        self.assertEqual(
            clause["events"][0]["accepted_headword"]["event_id"], "E-0001"
        )
        self.assertEqual(clause["events"][0]["occurrence"]["tag"]["event_cut_id"], "cut-1")
        self.assertEqual(clause["quantified_statement_tags"][0]["id"], "Q-0001")
        self.assertEqual(clause["unresolved_event_suggestions"][0]["event_id"] if "event_id" in clause["unresolved_event_suggestions"][0] else clause["unresolved_event_suggestions"][0]["id"], "E-0002")
