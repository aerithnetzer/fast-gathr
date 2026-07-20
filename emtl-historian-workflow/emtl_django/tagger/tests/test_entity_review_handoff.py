from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from tagger.models import Document, StageOutput
from tagger.services.entity_review_handoff import (
    build_entity_downstream_package,
    accept_remaining_entity_review_rows,
    export_entity_review_file,
    import_entity_review_file,
    propose_entity,
)
from tagger.services.stage_runner import _accepted_upstream_stage_ids, _stage_input_text


class EntityReviewHandoffTests(TestCase):
    def setUp(self) -> None:
        document = Document.objects.create(doc_id="entity-review-test", title="Review test")
        self.original_rows = [
            {"type": "P", "id": "P-0001", "headword": "Alpha", "raw_line": "P: Alpha [P-0001]"},
            {"type": "L", "id": "L-0002", "headword": "Beta", "raw_line": "L: Beta [L-0002]"},
            {"type": "I", "id": "I-0003", "headword": "Gamma", "raw_line": "I: Gamma [I-0003]"},
        ]
        self.stage_output = StageOutput.objects.create(
            document=document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            status=StageOutput.Status.CHECKING,
            display_title="Entity Registry",
            raw_output="immutable raw Qwen output",
            payload={
                "entity_output": {"tags": self.original_rows},
                "entity_validation": {
                    "registry_version": "registry-v-test",
                    "resource_hashes": {"Entity_List.xlsx": "hash-test"},
                },
                "entity_registry": self.original_rows,
                "entity_review": {
                    "state": "review_candidate",
                    "attempt_id": 7,
                    "approved_for_downstream": False,
                },
            },
            provenance={"entity_registry": {"approved_for_downstream": False}},
        )

    def test_export_review_import_and_downstream_contract(self) -> None:
        self.assertNotIn("entity_registry", _accepted_upstream_stage_ids({"entity_registry": self.stage_output}))
        with self.assertRaises(ValueError):
            build_entity_downstream_package(self.stage_output)

        with TemporaryDirectory() as directory:
            review_path = Path(directory) / "entity-review.json"
            exported = export_entity_review_file(stage_output=self.stage_output, output_path=review_path)
            exported["rows"][0]["decision"] = "accepted"
            exported["rows"][1]["decision"] = "edited"
            exported["rows"][1]["edited_row"]["headword"] = "Beta reviewed"
            exported["rows"][2]["decision"] = "rejected"
            review_path.write_text(json.dumps(exported), encoding="utf-8")

            with self.assertRaises(ValueError):
                import_entity_review_file(
                    stage_output=self.stage_output,
                    input_path=review_path,
                    confirm_approve_for_downstream=False,
                )
            package = import_entity_review_file(
                stage_output=self.stage_output,
                input_path=review_path,
                confirm_approve_for_downstream=True,
            )

        self.stage_output.refresh_from_db()
        self.assertEqual(self.stage_output.raw_output, "immutable raw Qwen output")
        self.assertEqual(self.stage_output.payload["entity_output"]["tags"], self.original_rows)
        self.assertEqual(self.stage_output.status, StageOutput.Status.ACCEPTED)
        self.assertTrue(self.stage_output.payload["entity_review"]["approved_for_downstream"])
        review_rows = self.stage_output.payload["entity_review"]["review_rows"]
        self.assertEqual([row["decision"] for row in review_rows], ["accepted", "edited", "rejected"])
        self.assertIsNone(review_rows[2]["reviewed_row"])
        self.assertEqual(len(package["reviewed_rows"]), 2)
        self.assertEqual(package["reviewed_rows"][1]["headword"], "Beta reviewed")
        self.assertEqual(package["source_attempt_id"], 7)
        self.assertEqual(package["registry_version"], "registry-v-test")
        self.assertEqual(package["registry_hashes"]["Entity_List.xlsx"], "hash-test")
        self.assertIn("entity_registry", _accepted_upstream_stage_ids({"entity_registry": self.stage_output}))
        occurrence_input = _stage_input_text(
            stage_id="occurrences_registry",
            document_header="Header",
            source_body="Body",
            stage_outputs={"entity_registry": self.stage_output},
        )
        self.assertIn("Beta reviewed", occurrence_input)
        self.assertNotIn("Gamma", occurrence_input)

    def test_accept_remaining_preserves_edit_and_reject(self) -> None:
        review = {
            "rows": [
                {"decision": "pending"},
                {"decision": "edited", "edited_row": {"headword": "Changed"}},
                {"decision": "rejected"},
            ]
        }
        updated, count = accept_remaining_entity_review_rows(review)
        self.assertEqual(count, 1)
        self.assertEqual(
            [row["decision"] for row in updated["rows"]],
            ["accepted", "edited", "rejected"],
        )

    def test_user_can_propose_entity_and_approved_proposal_enters_downstream(self) -> None:
        proposal = propose_entity(
            stage_output=self.stage_output,
            record_type="P",
            headword="User Person",
            evidence_form="the said person",
        )
        self.assertEqual(proposal.status, "pending")
        self.assertEqual(proposal.proposed_id, "NEW-P-0001")
        proposal.status = "approved"
        proposal.save(update_fields=["status", "updated_at"])
        payload = self.stage_output.payload
        payload["reviewed_entity_registry"] = []
        payload["entity_review"].update({
            "contract_version": "entity-human-review-v1",
            "state": "approved",
            "approved_for_downstream": True,
            "review_rows": [],
        })
        self.stage_output.payload = payload
        self.stage_output.provenance = {"entity_registry": {"approved_for_downstream": True}}
        self.stage_output.status = "accepted"
        self.stage_output.save()
        package = build_entity_downstream_package(self.stage_output)
        self.assertEqual(package["reviewed_rows"][0]["headword"], "User Person")
        self.assertEqual(package["reviewed_rows"][0]["review_decision"], "proposed_and_accepted")
