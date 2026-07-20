from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
DJANGO_ROOT = ROOT / "emtl_django"
if str(DJANGO_ROOT) not in sys.path:
    sys.path.insert(0, str(DJANGO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emtl_site.settings")

import django  # noqa: E402

django.setup()

from django.core.management import CommandError, call_command  # noqa: E402
from django.test import TestCase  # noqa: E402

from tagger.models import Clause, Document, StageOutput  # noqa: E402
from tagger.services.contracts import STAGE_CONTRACT_VERSION  # noqa: E402
from tagger.services.stage_validation import CLAUSE_OUTPUT_CONTRACT_VERSION  # noqa: E402


HEADER = "DocID: revalidation-test\nDocument Type: deposition\n<END>"
BODY = "First body sentence. Second body sentence."
RAW_OUTPUT = (
    f"[{HEADER}]\n\n"
    "CLAUSE 001\nFirst body sentence.\n\n"
    "CLAUSE 002\nSecond body sentence."
)


class ClauseStageRevalidationCommandTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(
            doc_id="revalidation-test",
            title="Revalidation test",
            document_type="deposition",
            metadata={
                "working_source_text": f"{HEADER}\n\n{BODY}",
                "preserved_metadata": {"value": 7},
            },
        )
        for sequence, text in enumerate(("old one", "old two", "old three"), start=1):
            Clause.objects.create(
                document=self.document,
                clause_id=f"old-{sequence:04d}",
                text=text,
                sequence=sequence,
                start_char=sequence * 10,
                end_char=sequence * 10 + len(text),
            )
        working_source = self.document.metadata["working_source_text"].strip()
        self.stage_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.CHECKING,
            display_title="Clause Parser",
            raw_output=RAW_OUTPUT,
            payload={
                "clauses": [{"clause_id": "001", "sequence": 1, "text": "stale"}],
                "coverage_validation": {"valid": False},
                "generated_output": RAW_OUTPUT,
                "notice": "stale validation",
                "provider_payload": {
                    "token_counts": {"input_tokens": 100, "output_tokens": 50},
                    "generation_timing": {"elapsed_seconds": 1.25},
                    "response": {"finish_reason": "stop"},
                },
                "runner_contract": {"provider": "gpu_local"},
            },
            provenance={
                "source": "chatbot_stage_runner",
                "contract_version": STAGE_CONTRACT_VERSION,
                "stage_id": "clause_parser",
                "execution_status": "completed",
                "finished_at": "2026-06-28T09:00:00+00:00",
                "provider": "gpu_local",
                "model": "test-model",
                "errors": [],
                "request": {
                    "stage_id": "clause_parser",
                    "document_id": self.document.doc_id,
                    "contract_version": STAGE_CONTRACT_VERSION,
                    "source_character_count": len(working_source),
                },
                "provider_api_payload": {
                    "inputs": {"document_body_character_count": len(working_source)}
                },
                "provider_response_metadata": {"preserve": True},
            },
        )

    def test_dry_run_reports_exact_plan_without_writing(self) -> None:
        payload_before = copy.deepcopy(self.stage_output.payload)
        provenance_before = copy.deepcopy(self.stage_output.provenance)
        updated_at_before = self.stage_output.updated_at
        document_before = self._document_snapshot()
        stream = io.StringIO()

        with patch(
            "tagger.services.providers.gpu_local.GpuLocalProviderClient.generate"
        ) as provider_generate:
            call_command(
                "revalidate_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                dry_run=True,
                stdout=stream,
            )

        provider_generate.assert_not_called()
        report = json.loads(stream.getvalue())
        self.stage_output.refresh_from_db()
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["database_write_performed"])
        self.assertEqual(report["parsed"]["clause_count"], 2)
        self.assertTrue(report["validation"]["valid"])
        self.assertIn("output_contract_version", report["payload_changed_paths"])
        self.assertIn("generated_header", report["payload_changed_paths"])
        self.assertEqual(self.stage_output.payload, payload_before)
        self.assertEqual(self.stage_output.provenance, provenance_before)
        self.assertEqual(self.stage_output.updated_at, updated_at_before)
        self.assertEqual(self._document_snapshot(), document_before)

    def test_write_is_scoped_and_second_execution_is_idempotent(self) -> None:
        raw_before = self.stage_output.raw_output
        updated_at_before = self.stage_output.updated_at
        provider_payload_before = copy.deepcopy(self.stage_output.payload["provider_payload"])
        provenance_before = copy.deepcopy(self.stage_output.provenance)
        document_before = self._document_snapshot()

        first_stream = io.StringIO()
        call_command(
            "revalidate_clause_stage_output",
            stage_output_id=self.stage_output.pk,
            stdout=first_stream,
        )
        first_report = json.loads(first_stream.getvalue())
        self.stage_output.refresh_from_db()

        self.assertTrue(first_report["database_write_performed"])
        self.assertFalse(first_report["idempotent_noop"])
        self.assertEqual(
            self.stage_output.payload["output_contract_version"],
            CLAUSE_OUTPUT_CONTRACT_VERSION,
        )
        self.assertEqual(self.stage_output.payload["generated_header"], HEADER)
        self.assertTrue(self.stage_output.payload["header_was_bracket_wrapped"])
        self.assertEqual(len(self.stage_output.payload["clauses"]), 2)
        self.assertTrue(self.stage_output.payload["coverage_validation"]["valid"])
        self.assertTrue(
            self.stage_output.payload["coverage_validation"]["header_validation"]["valid"]
        )
        self.assertTrue(
            self.stage_output.payload["coverage_validation"]["body_validation"]["valid"]
        )
        self.assertEqual(self.stage_output.payload["provider_payload"], provider_payload_before)
        self.assertEqual(self.stage_output.raw_output, raw_before)
        self.assertEqual(self.stage_output.updated_at, updated_at_before)
        self.assertEqual(self.stage_output.status, StageOutput.Status.CHECKING)
        self.assertEqual(self._document_snapshot(), document_before)
        self.assertFalse(self.stage_output.provenance["offline_revalidation"]["model_called"])
        self.assertFalse(
            self.stage_output.provenance["offline_revalidation"][
                "clauses_applied_to_document"
            ]
        )
        for key, value in provenance_before.items():
            self.assertEqual(self.stage_output.provenance[key], value)

        payload_after_first = copy.deepcopy(self.stage_output.payload)
        provenance_after_first = copy.deepcopy(self.stage_output.provenance)
        updated_at_after_first = self.stage_output.updated_at
        second_stream = io.StringIO()
        call_command(
            "revalidate_clause_stage_output",
            stage_output_id=self.stage_output.pk,
            stdout=second_stream,
        )
        second_report = json.loads(second_stream.getvalue())
        self.stage_output.refresh_from_db()

        self.assertFalse(second_report["database_write_performed"])
        self.assertTrue(second_report["idempotent_noop"])
        self.assertEqual(self.stage_output.payload, payload_after_first)
        self.assertEqual(self.stage_output.provenance, provenance_after_first)
        self.assertEqual(self.stage_output.updated_at, updated_at_after_first)
        self.assertEqual(self._document_snapshot(), document_before)

    def test_refuses_non_completed_execution(self) -> None:
        provenance = dict(self.stage_output.provenance)
        provenance["execution_status"] = "error"
        self.stage_output.provenance = provenance
        self.stage_output.save(update_fields=["provenance"])

        with self.assertRaisesMessage(CommandError, "execution_status must be completed"):
            call_command(
                "revalidate_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                dry_run=True,
            )

    def _document_snapshot(self) -> dict[str, object]:
        self.document.refresh_from_db()
        return {
            "metadata": copy.deepcopy(self.document.metadata),
            "updated_at": self.document.updated_at,
            "clauses": list(
                self.document.clauses.order_by("sequence", "pk").values(
                    "id", "clause_id", "text", "sequence", "start_char", "end_char"
                )
            ),
        }
