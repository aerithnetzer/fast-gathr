from __future__ import annotations

import copy
import io
import json
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from tagger.models import Clause, Document, StageOutput
from tagger.services.clause_application import (
    _validated_payload_clauses,
    generation_provenance,
    text_sha256,
)
from tagger.services.stage_validation import (
    CLAUSE_OUTPUT_CONTRACT_VERSION,
    validate_clause_coverage,
)


HEADER = "DocID: application-test\nDocument Type: deposition\n<END>"
PARTS = [f"Body part {index:02d}." for index in range(1, 16)]
BODY = " ".join(PARTS)
CLAUSES = [
    {"clause_id": f"{index:03d}", "sequence": index, "text": text}
    for index, text in enumerate(PARTS, start=1)
]
RAW_OUTPUT = f"[{HEADER}]\n\n" + "\n\n".join(
    f"CLAUSE {clause['clause_id']}\n{clause['text']}" for clause in CLAUSES
)


class ClauseStageApplicationCommandTests(TestCase):
    def test_payload_clause_count_is_document_specific(self) -> None:
        clauses = [
            {"clause_id": f"{index:03d}", "sequence": index, "text": f"Part {index}."}
            for index in range(1, 14)
        ]
        self.assertEqual(_validated_payload_clauses(clauses), clauses)

    def setUp(self) -> None:
        self.working_source = f"{HEADER}\n\n{BODY}"
        self.document = Document.objects.create(
            doc_id="application-test",
            title="Application test",
            document_type="deposition",
            source_file="application-test.docx",
            metadata={
                "working_source_text": self.working_source,
                "preserved": {"value": 11},
            },
        )
        for sequence in range(1, 36):
            Clause.objects.create(
                document=self.document,
                clause_id=f"{sequence:04d}",
                sequence=sequence,
                text=f"Existing fixture clause {sequence}.",
                start_char=sequence * 10,
                end_char=sequence * 10 + 5,
            )
        coverage = validate_clause_coverage(
            BODY,
            CLAUSES,
            expected_header=HEADER,
            generated_header=HEADER,
        ).as_dict()
        self.stage_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.CHECKING,
            display_title="Clause Parser",
            raw_output=RAW_OUTPUT,
            payload={
                "output_contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
                "generated_header": HEADER,
                "header_was_bracket_wrapped": True,
                "clauses": copy.deepcopy(CLAUSES),
                "coverage_validation": coverage,
                "generated_output": RAW_OUTPUT,
                "provider_payload": {
                    "token_counts": {"input": 500, "output": 200},
                    "generation_timing": {"elapsed": 2.5},
                },
            },
            provenance={
                "source": "chatbot_stage_runner",
                "stage_id": "clause_parser",
                "execution_status": "completed",
                "real_chatbot_execution": True,
                "provider": "gpu_local",
                "model": "frozen-test-model",
                "errors": [],
                "provider_response_metadata": {"finish_reason": "stop"},
                "offline_revalidation": {
                    "stage_output_id": None,
                    "contract_version": CLAUSE_OUTPUT_CONTRACT_VERSION,
                    "raw_output_sha256": text_sha256(RAW_OUTPUT),
                    "structured_input_sha256": text_sha256(self.working_source),
                    "expected_header_sha256": text_sha256(HEADER),
                    "expected_body_sha256": text_sha256(BODY),
                    "provider_called": False,
                    "model_called": False,
                },
            },
        )
        provenance = copy.deepcopy(self.stage_output.provenance)
        provenance["offline_revalidation"]["stage_output_id"] = self.stage_output.pk
        self.stage_output.provenance = provenance
        self.stage_output.save(update_fields=["provenance"])

        self.other_document = Document.objects.create(
            doc_id="application-scope-other",
            title="Scope control",
            metadata={"working_source_text": "Other source."},
        )
        Clause.objects.create(
            document=self.other_document,
            clause_id="0001",
            sequence=1,
            text="Other clause.",
        )
        self.other_stage = StageOutput.objects.create(
            document=self.other_document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
            status=StageOutput.Status.LOADED,
            display_title="Entity Registry",
            raw_output="Other output.",
            payload={"preserve": True},
            provenance={"source": "scope-control"},
        )

    def test_dry_run_reports_35_to_15_without_writing(self) -> None:
        before = self._full_snapshot()
        stream = io.StringIO()

        with patch(
            "tagger.services.providers.gpu_local.GpuLocalProviderClient.generate"
        ) as provider_generate:
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                dry_run=True,
                stdout=stream,
            )

        provider_generate.assert_not_called()
        report = json.loads(stream.getvalue())
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["database_write_performed"])
        self.assertEqual(report["before"]["clause_count"], 35)
        self.assertEqual(report["after"]["clause_count"], 15)
        self.assertEqual(report["before"]["clause_ids"], [f"{i:04d}" for i in range(1, 36)])
        self.assertEqual(report["after"]["clause_ids"], [f"{i:03d}" for i in range(1, 16)])
        self.assertTrue(report["coverage"]["valid"])
        self.assertTrue(report["header"]["exact_match"])
        self.assertEqual(report["after"]["lifecycle_status"], StageOutput.Status.ACCEPTED)
        self.assertEqual(self._full_snapshot(), before)

    def test_precondition_rejections_do_not_write(self) -> None:
        original_payload = copy.deepcopy(self.stage_output.payload)
        before = self._full_snapshot()

        payload = copy.deepcopy(original_payload)
        payload["coverage_validation"]["body_validation"]["valid"] = False
        self.stage_output.payload = payload
        self.stage_output.save(update_fields=["payload"])
        rejected_state = self._full_snapshot()
        with self.assertRaisesMessage(CommandError, "body validation is not valid"):
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                dry_run=True,
            )
        self.assertEqual(self._full_snapshot(), rejected_state)

        self.stage_output.payload = original_payload
        self.stage_output.save(update_fields=["payload"])
        provenance = copy.deepcopy(self.stage_output.provenance)
        provenance["real_chatbot_execution"] = False
        self.stage_output.provenance = provenance
        self.stage_output.save(update_fields=["provenance"])
        rejected_state = self._full_snapshot()
        with self.assertRaisesMessage(CommandError, "real_chatbot_execution must be true"):
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                dry_run=True,
            )
        self.assertEqual(self._full_snapshot(), rejected_state)
        self.assertNotEqual(self._full_snapshot(), before)

    def test_transaction_rolls_back_partial_clause_deletion(self) -> None:
        before = self._full_snapshot()

        def fail_after_delete(document, clauses) -> None:
            document.clauses.all().delete()
            raise RuntimeError("forced persistence failure")

        with patch(
            "tagger.management.commands.apply_clause_stage_output.replace_document_clauses",
            side_effect=fail_after_delete,
        ), self.assertRaisesMessage(RuntimeError, "forced persistence failure"):
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
            )

        self.assertEqual(self._full_snapshot(), before)

    def test_success_is_scoped_and_uses_formal_persistence(self) -> None:
        metadata_before = copy.deepcopy(self.document.metadata)
        raw_before = self.stage_output.raw_output
        payload_before = copy.deepcopy(self.stage_output.payload)
        generation_before = generation_provenance(self.stage_output.provenance)
        other_document_before = self._other_document_snapshot()
        other_stage_before = self._other_stage_snapshot()
        stream = io.StringIO()

        with patch(
            "tagger.management.commands.apply_clause_stage_output.replace_document_clauses",
            wraps=__import__(
                "tagger.services.clause_persistence",
                fromlist=["replace_document_clauses"],
            ).replace_document_clauses,
        ) as persistence:
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                stdout=stream,
            )

        persistence.assert_called_once()
        report = json.loads(stream.getvalue())
        self.stage_output.refresh_from_db()
        self.document.refresh_from_db()
        persisted = list(
            self.document.clauses.order_by("sequence", "pk").values(
                "clause_id", "sequence", "text", "start_char", "end_char"
            )
        )
        self.assertTrue(report["database_write_performed"])
        self.assertFalse(report["idempotent_noop"])
        self.assertEqual(self.stage_output.status, StageOutput.Status.ACCEPTED)
        self.assertEqual(len(persisted), 15)
        self.assertEqual(
            [{key: row[key] for key in ("clause_id", "sequence", "text")} for row in persisted],
            CLAUSES,
        )
        self.assertTrue(all(row["start_char"] is None and row["end_char"] is None for row in persisted))
        self.assertTrue(self.stage_output.provenance["clauses_applied_to_document"])
        self.assertEqual(
            self.stage_output.provenance["clause_application"]["after_clause_count"],
            15,
        )
        self.assertEqual(self.document.metadata, metadata_before)
        self.assertEqual(self.stage_output.raw_output, raw_before)
        self.assertEqual(self.stage_output.payload, payload_before)
        self.assertEqual(generation_provenance(self.stage_output.provenance), generation_before)
        self.assertEqual(self._other_document_snapshot(), other_document_before)
        self.assertEqual(self._other_stage_snapshot(), other_stage_before)
        self.assertTrue(all(report["integrity"].values()))

    def test_second_execution_is_idempotent_without_clause_replacement(self) -> None:
        call_command(
            "apply_clause_stage_output",
            stage_output_id=self.stage_output.pk,
            stdout=io.StringIO(),
        )
        before = self._full_snapshot()
        clause_pks_before = list(
            self.document.clauses.order_by("sequence", "pk").values_list("pk", flat=True)
        )
        stream = io.StringIO()

        with patch(
            "tagger.management.commands.apply_clause_stage_output.replace_document_clauses"
        ) as persistence:
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                stdout=stream,
            )

        persistence.assert_not_called()
        report = json.loads(stream.getvalue())
        self.assertFalse(report["database_write_performed"])
        self.assertTrue(report["idempotent_noop"])
        self.assertEqual(self._full_snapshot(), before)
        self.assertEqual(
            list(self.document.clauses.order_by("sequence", "pk").values_list("pk", flat=True)),
            clause_pks_before,
        )

    def test_preapplied_checking_result_is_accepted_without_second_replacement(self) -> None:
        self.document.clauses.all().delete()
        for clause in CLAUSES:
            Clause.objects.create(
                document=self.document,
                clause_id=clause["clause_id"],
                sequence=clause["sequence"],
                text=clause["text"],
            )
        provenance = copy.deepcopy(self.stage_output.provenance)
        provenance["clauses_applied_to_document"] = True
        self.stage_output.provenance = provenance
        self.stage_output.save(update_fields=["provenance"])
        stream = io.StringIO()

        with patch(
            "tagger.management.commands.apply_clause_stage_output.replace_document_clauses"
        ) as persistence:
            call_command(
                "apply_clause_stage_output",
                stage_output_id=self.stage_output.pk,
                stdout=stream,
            )

        persistence.assert_not_called()
        report = json.loads(stream.getvalue())
        self.stage_output.refresh_from_db()
        self.assertTrue(report["clauses_already_materialized"])
        self.assertFalse(report["clause_replacement_performed"])
        self.assertEqual(self.stage_output.status, StageOutput.Status.ACCEPTED)
        self.assertIn("clause_application", self.stage_output.provenance)

    def _full_snapshot(self) -> dict[str, object]:
        self.stage_output.refresh_from_db()
        self.document.refresh_from_db()
        return {
            "document": {
                "metadata": copy.deepcopy(self.document.metadata),
                "title": self.document.title,
                "source_file": self.document.source_file,
                "updated_at": self.document.updated_at,
            },
            "clauses": list(
                self.document.clauses.order_by("sequence", "pk").values()
            ),
            "stage": {
                "status": self.stage_output.status,
                "raw_output": self.stage_output.raw_output,
                "payload": copy.deepcopy(self.stage_output.payload),
                "provenance": copy.deepcopy(self.stage_output.provenance),
                "updated_at": self.stage_output.updated_at,
            },
            "other_document": self._other_document_snapshot(),
            "other_stage": self._other_stage_snapshot(),
        }

    def _other_document_snapshot(self) -> dict[str, object]:
        self.other_document.refresh_from_db()
        return {
            "metadata": copy.deepcopy(self.other_document.metadata),
            "updated_at": self.other_document.updated_at,
            "clauses": list(self.other_document.clauses.order_by("sequence", "pk").values()),
        }

    def _other_stage_snapshot(self) -> dict[str, object]:
        self.other_stage.refresh_from_db()
        return {
            "status": self.other_stage.status,
            "raw_output": self.other_stage.raw_output,
            "payload": copy.deepcopy(self.other_stage.payload),
            "provenance": copy.deepcopy(self.other_stage.provenance),
            "updated_at": self.other_stage.updated_at,
        }
