import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from tagger.models import Document, StageExecutionAttempt, StageOutput
from tagger.services.export_contract import (
    SCHEMA_VERSION,
    build_workflow_export,
    validate_workflow_export,
    write_workflow_export,
)


class WorkflowExportContractTests(TestCase):
    def setUp(self):
        self.document = Document.objects.create(doc_id="export-doc", title="Export document")
        self.document.clauses.create(clause_id="001", sequence=1, text="Original text")
        self.summary = StageOutput.objects.create(
            document=self.document,
            stage="summary_keywords",
            status="accepted",
            display_title="Summary",
            raw_output="SUMMARY\nAccepted",
            payload={"summary": "Accepted"},
        )
        self.occurrence = StageOutput.objects.create(
            document=self.document,
            stage="occurrences_registry",
            status="checking",
            display_title="Occurrences",
            raw_output="E: candidate",
            payload={"review_state": "review_candidate"},
        )
        StageExecutionAttempt.objects.create(
            stage_output=self.occurrence,
            request_id="export-attempt-1",
            stage="occurrences_registry",
            execution_status="completed",
            disposition="applied_to_checking",
            provider="gpu_local",
            raw_output="E: candidate",
        )

    def test_accepted_and_audit_layers_are_separated(self):
        package = build_workflow_export(self.document)
        self.assertEqual(package["schema_version"], SCHEMA_VERSION)
        self.assertIn("summary_keywords", package["accepted_data"]["stage_outputs"])
        self.assertNotIn("occurrences_registry", package["accepted_data"]["stage_outputs"])
        self.assertEqual(package["accepted_data"]["occurrences"], {})
        self.assertEqual(len(package["audit"]["execution_attempts"]), 1)
        self.assertIn("occurrences_registry", package["integrity"]["nonaccepted_stage_ids_audit_only"])
        self.assertTrue(validate_workflow_export(package)["valid"])

    def test_write_produces_json_package(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            package = write_workflow_export(document=self.document, output_path=path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["export_id"], package["export_id"])
        self.assertEqual(loaded["document"]["document_id"], "export-doc")

    def test_validator_rejects_nonaccepted_data_in_accepted_layer(self):
        package = build_workflow_export(self.document)
        package["accepted_data"]["occurrences"] = {
            "stage_id": "occurrences_registry", "status": "checking"
        }
        self.assertFalse(validate_workflow_export(package)["valid"])

    def test_validator_detects_tampering(self):
        package = build_workflow_export(self.document)
        package["document"]["title"] = "Tampered"
        self.assertIn(
            "integrity.canonical_json_sha256",
            validate_workflow_export(package)["issues"],
        )

    def test_entity_rows_and_event_headword_assignments_are_first_class_decisions(self):
        StageOutput.objects.create(
            document=self.document,
            stage="entity_registry",
            status="accepted",
            display_title="Entity Registry",
            payload={
                "entity_review": {
                    "contract_version": "entity-review-handoff-v1",
                    "state": "approved", "approved_for_downstream": True,
                    "reviewed_at": "2026-07-01T12:00:00+00:00",
                    "review_rows": [{
                        "review_row_id": "entity-0001", "decision": "edited",
                        "original_row": {"id": "P-1", "headword": "Alice"},
                        "reviewed_row": {"id": "P-9", "headword": "Alice"},
                    }],
                },
                "reviewed_entity_registry": [{"id": "P-9", "headword": "Alice"}],
            },
            provenance={"entity_registry": {"approved_for_downstream": True}},
        )
        StageOutput.objects.create(
            document=self.document,
            stage="eventcut_extraction",
            status="accepted",
            display_title="Event Extraction",
            payload={
                "accepted_assignments": [{"event_id": "E-0103", "headword": "Gift"}],
                "headword_review_store": {"items": {"review-1": {
                    "item_id": "review-1", "revision": 1,
                    "state": "accepted_existing_headword",
                    "event_cut": {"event_cut_id": "eventcut-1", "text": "gave a gift"},
                    "chooser_selected_candidate": {"event_id": "E-0103", "headword": "Gift"},
                    "assignment": {
                        "status": "accepted", "event_id": "E-0103", "headword": "Gift",
                        "accepted_by": "historian", "accepted_at": "2026-07-01T12:05:00+00:00",
                    },
                    "audit_log": [{"action": "accept"}],
                }}},
            },
        )
        package = build_workflow_export(self.document)
        decisions = package["review"]["decisions"]
        self.assertTrue(any(row["target_type"] == "entity_row" for row in decisions))
        self.assertTrue(any(row["target_type"] == "event_headword_assignment" for row in decisions))
        self.assertEqual(package["accepted_data"]["event_assignments"][0]["event_id"], "E-0103")
