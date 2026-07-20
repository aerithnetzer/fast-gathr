from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.test import TestCase

from tagger.models import Document, StageOutput
from tagger.services.occurrence_generation import (
    OccurrenceGenerationError,
    _accepted_clause_records,
    _approved_entity_package,
    validate_occurrence_enrichment_output,
    validate_occurrence_output,
)


class OccurrenceGenerationTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(doc_id="doc-1", title="Doc")
        self.clause = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.ACCEPTED,
            display_title="Clause",
            payload={
                "clauses": [
                    {"clause_id": "001", "sequence": 1, "text": "he gave money"},
                    {"clause_id": "002", "sequence": 2, "text": "she was alive"},
                ]
            },
        )
        self.entity = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            status=StageOutput.Status.ACCEPTED,
            display_title="Entity",
            payload={
                "entity_review": {
                    "contract_version": "entity-human-review-v1",
                    "state": "approved",
                    "approved_for_downstream": True,
                    "review_rows": [],
                },
                "reviewed_entity_registry": [
                    {"type": "P", "headword": "John", "id": "P-0001", "fields": []}
                ],
                "entity_validation": {},
            },
            provenance={"entity_registry": {"approved_for_downstream": True}},
        )

    def test_gate_requires_accepted_clause(self) -> None:
        self.clause.status = StageOutput.Status.CHECKING
        self.clause.save(update_fields=["status"])
        with self.assertRaisesRegex(OccurrenceGenerationError, "accepted Clause"):
            _accepted_clause_records(self.clause, ["001"])

    def test_gate_requires_explicit_entity_approval(self) -> None:
        self.entity.payload["entity_review"]["approved_for_downstream"] = False
        self.entity.save(update_fields=["payload"])
        with self.assertRaisesRegex(OccurrenceGenerationError, "human-reviewed"):
            _approved_entity_package(self.clause, self.entity)

    def test_valid_output_is_clause_and_trigger_anchored(self) -> None:
        result = validate_occurrence_output(
            raw_output=(
                "CLAUSE 001\n\nhe gave money\n\n"
                "E: Give [E-0001] | Trigger: gave | Actor: John [P-0001]"
            ),
            clauses=[{"clause_id": "001", "text": "he gave money"}],
            approved_entity_rows=[{"id": "P-0001"}],
            allowed_authority_ids={"E-0001"},
            authority_headwords={"E-0001": "Give"},
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["tag_count"], 1)

    def test_unapproved_entity_and_unanchored_trigger_fail(self) -> None:
        result = validate_occurrence_output(
            raw_output=(
                "CLAUSE 001\n\nhe gave money\n\n"
                "E: Give [E-0001] | Trigger: delivered | Actor: Jane [P-9999]"
            ),
            clauses=[{"clause_id": "001", "text": "he gave money"}],
            approved_entity_rows=[{"id": "P-0001"}],
            allowed_authority_ids={"E-0001"},
            authority_headwords={"E-0001": "Give"},
        )
        self.assertFalse(result["valid"])
        self.assertEqual(
            {issue["code"] for issue in result["issues"]},
            {"trigger_not_in_clause", "unapproved_entity_reference"},
        )

    def test_existing_id_requires_canonical_headword(self) -> None:
        result = validate_occurrence_output(
            raw_output="CLAUSE 001\n\nhe gave money\n\nE: Gift [E-0001] | Trigger: gave",
            clauses=[{"clause_id": "001", "text": "he gave money"}],
            approved_entity_rows=[],
            allowed_authority_ids={"E-0001"},
            authority_headwords={"E-0001": "Give"},
        )
        self.assertEqual(result["issues"][0]["code"], "authority_headword_mismatch")

    def test_enrichment_keeps_approved_event_and_routes_extra_event_back(self) -> None:
        result = validate_occurrence_enrichment_output(
            raw_output=(
                "CLAUSE 001\n\nhe gave and paid money\n\n"
                "E: Give [E-0001] | Trigger: gave | Actor: John [P-0001]\n"
                "E: Pay [E-0002] | Trigger: paid | Actor: John [P-0001]"
            ),
            clauses=[{"clause_id": "001", "text": "he gave and paid money"}],
            approved_entity_rows=[{"id": "P-0001"}],
            allowed_authority_ids={"E-0001", "E-0002"},
            authority_headwords={"E-0001": "Give", "E-0002": "Pay"},
            approved_event_assignments=[
                {
                    "assignment_id": "assignment-1",
                    "event_cut_id": "cut-1",
                    "clause_id": "001",
                    "event_id": "E-0001",
                    "headword": "Give",
                }
            ],
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["accepted_event_occurrence_count"], 1)
        self.assertEqual(result["parsed_clauses"][0]["tags"][0]["event_cut_id"], "cut-1")
        self.assertEqual(result["unresolved_event_suggestion_count"], 1)
        self.assertEqual(
            result["parsed_clauses"][0]["unresolved_event_suggestions"][0]["event_id"],
            "E-0002",
        )

    def test_enrichment_fails_when_approved_event_is_omitted(self) -> None:
        result = validate_occurrence_enrichment_output(
            raw_output="CLAUSE 001\n\nhe gave money\n\nQ: Quantity [Q-0007] | Trigger: money",
            clauses=[{"clause_id": "001", "text": "he gave money"}],
            approved_entity_rows=[],
            allowed_authority_ids={"Q-0007", "E-0001"},
            authority_headwords={"Q-0007": "Quantity", "E-0001": "Give"},
            approved_event_assignments=[
                {
                    "assignment_id": "assignment-1",
                    "event_cut_id": "cut-1",
                    "clause_id": "001",
                    "event_id": "E-0001",
                    "headword": "Give",
                }
            ],
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "accepted_event_missing_occurrence",
            {issue["code"] for issue in result["issues"]},
        )
