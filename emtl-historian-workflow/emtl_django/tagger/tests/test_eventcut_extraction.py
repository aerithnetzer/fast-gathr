from __future__ import annotations

import json
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from tagger.models import Document, StageExecutionAttempt, StageOutput
from tagger.services.eventcut_extraction import (
    DOWNSTREAM_CONTRACT_VERSION,
    INTERNAL_CONTRACT_VERSION,
    EventCutExtractionRunner,
    build_downstream_package,
    build_eventcut_prompt,
    clause_records_from_output,
    parse_eventcut_output,
    validate_eventcuts,
)
from tagger.services.providers.gpu_local import GpuLocalProviderResult


CLAUSE_3 = (
    "the sayde ffraunces kightley dyed indebted to the ladye Laxston "
    "which somme was borowed of the sayde ladye laxston"
)
CLAUSE_5 = (
    "the sayde ladye laxston was contented and dyd agree to forbeare the same "
    "somme and they shoulde enter into newe bandes"
)


def source_clauses():
    return [
        {
            "clause_id": "003",
            "sequence": 3,
            "text": CLAUSE_3,
            "text_sha256": "hash-3",
        },
        {
            "clause_id": "005",
            "sequence": 5,
            "text": CLAUSE_5,
            "text_sha256": "hash-5",
        },
    ]


class EventCutPureContractTests(TestCase):
    def test_prompt_contains_rules_and_selected_clause_text(self) -> None:
        prompt = build_eventcut_prompt(source_clauses()[:1])
        rendered = prompt["system_prompt"] + prompt["user_prompt"]
        self.assertIn("smaller than a Clause", rendered)
        self.assertIn("Never modernize spelling", rendered)
        self.assertIn("Do not assign an Event headword", rendered)
        self.assertIn(CLAUSE_3, rendered)
        self.assertNotIn(CLAUSE_5, rendered)

    def test_parser_handles_multiple_eventcuts_from_one_clause(self) -> None:
        raw = json.dumps(
            {
                "contract_version": "eventcut-llm-output-v1",
                "event_cuts": [
                    {
                        "clause_id": "003",
                        "event_cut_text": "dyed indebted to the ladye Laxston",
                        "trigger": "indebted",
                    },
                    {
                        "clause_id": "003",
                        "event_cut_text": "was borowed of the sayde ladye laxston",
                        "trigger": "was borowed",
                    },
                ],
            }
        )
        self.assertEqual(len(parse_eventcut_output(raw)), 2)

    def test_validator_accepts_exact_spans_and_restores_narrative_order(self) -> None:
        cuts, report = validate_eventcuts(
            [
                {
                    "clause_id": "005",
                    "event_cut_text": "shoulde enter into newe bandes",
                    "trigger": "enter",
                },
                {
                    "clause_id": "003",
                    "event_cut_text": "was borowed of the sayde ladye laxston",
                    "trigger": "was borowed",
                },
            ],
            source_clauses(),
            doc_id="doc-1",
            source_stage_output_id=141,
        )
        self.assertTrue(report["valid"])
        self.assertTrue(report["llm_output_reordered_to_narrative_order"])
        self.assertEqual([item["clause_id"] for item in cuts], ["003", "005"])
        self.assertTrue(all(item["event_cut_id"].startswith("eventcut-") for item in cuts))

    def test_validator_rejects_modernized_non_substring_wrong_clause_and_trigger(self) -> None:
        cuts, report = validate_eventcuts(
            [
                {
                    "clause_id": "003",
                    "event_cut_text": "was borrowed of the lady Laxton",
                    "trigger": "borrowed",
                },
                {
                    "clause_id": "999",
                    "event_cut_text": "shoulde enter into newe bandes",
                },
                {
                    "clause_id": "005",
                    "event_cut_text": "shoulde enter into newe bandes",
                    "trigger": "contracted",
                },
            ],
            source_clauses(),
            doc_id="doc-1",
            source_stage_output_id=141,
        )
        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["valid"])
        self.assertIn("event_cut_not_exact_substring", codes)
        self.assertIn("unknown_clause_id", codes)
        self.assertIn("trigger_not_in_event_cut", codes)
        self.assertTrue(all(not item["valid"] for item in cuts))


class FakeEventCutClient:
    def __init__(self, raw_output: str) -> None:
        self.raw_output = raw_output
        self.calls = 0

    def generate(self, payload):
        self.calls += 1
        self.payload = payload
        return GpuLocalProviderResult(
            status="completed",
            raw_output=self.raw_output,
            provider="gpu_local",
            model="test-model",
            metadata={"provider_provenance": {"test": True}},
            real_chatbot_execution=True,
        )


class EventCutPersistenceAndExportTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(doc_id="doc-eventcut", title="Test")
        self.clause_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.ACCEPTED,
            display_title="Clause Parser",
            payload={
                "clauses": [
                    {"clause_id": "003", "sequence": 3, "text": CLAUSE_3},
                    {"clause_id": "005", "sequence": 5, "text": CLAUSE_5},
                ]
            },
        )

    def test_real_command_path_persists_internal_output_without_review_state(self) -> None:
        raw = json.dumps(
            {
                "contract_version": "eventcut-llm-output-v1",
                "event_cuts": [
                    {
                        "clause_id": "003",
                        "event_cut_text": "was borowed of the sayde ladye laxston",
                        "trigger": "was borowed",
                        "lookup_context_text": CLAUSE_3,
                    }
                ],
            }
        )
        client = FakeEventCutClient(raw)
        clauses = clause_records_from_output(self.clause_output, ["003"])
        summary, package = EventCutExtractionRunner(client=client).run(
            clause_output=self.clause_output,
            clauses=clauses,
            confirm_real_generation=True,
            request_id="eventcut-test-request",
        )
        stage_output = StageOutput.objects.get(
            document=self.document, stage=StageOutput.Stage.EVENTCUT_EXTRACTION
        )
        attempt = StageExecutionAttempt.objects.get(request_id="eventcut-test-request")
        self.clause_output.refresh_from_db()
        self.assertEqual(client.calls, 1)
        self.assertTrue(summary["internal_usable_for_lookup"])
        self.assertEqual(stage_output.status, StageOutput.Status.LOADED)
        self.assertEqual(package["contract_version"], INTERNAL_CONTRACT_VERSION)
        self.assertNotIn("review_state", package)
        self.assertFalse(package["validation_report"]["requires_user_review"])
        self.assertEqual(attempt.disposition, StageExecutionAttempt.Disposition.INTERNAL_APPLIED)
        self.assertEqual(self.clause_output.status, StageOutput.Status.ACCEPTED)

    def test_clause_selector_normalizes_single_comma_separated_string(self) -> None:
        clauses = clause_records_from_output(self.clause_output, "003, 005,003")
        self.assertEqual(
            [item["clause_id"] for item in clauses],
            ["003", "005"],
        )

    def test_clause_selector_normalizes_repeated_and_comma_separated_values(self) -> None:
        clauses = clause_records_from_output(
            self.clause_output, ["005", "003,005"]
        )
        self.assertEqual(
            [item["clause_id"] for item in clauses],
            ["003", "005"],
        )

    def test_command_normalizes_programmatic_string_selector(self) -> None:
        summary = {
            "internal_usable_for_lookup": True,
            "attempt_id": 1,
        }
        with patch(
            "tagger.management.commands.eventcut_gpu_extract.EventCutExtractionRunner"
        ) as runner_class:
            runner_class.return_value.run.return_value = (summary, {})
            call_command(
                "eventcut_gpu_extract",
                clause_stage_output_id=self.clause_output.pk,
                clause_ids="003,005",
                confirm_real_generation=True,
                stdout=io.StringIO(),
            )
        selected = runner_class.return_value.run.call_args.kwargs["clauses"]
        self.assertEqual([item["clause_id"] for item in selected], ["003", "005"])

    def test_downstream_export_contains_validated_eventcuts_only(self) -> None:
        stage_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION,
            status=StageOutput.Status.LOADED,
            display_title="EventCut Extraction (internal)",
            payload={
                "contract_version": INTERNAL_CONTRACT_VERSION,
                "doc_id": self.document.doc_id,
                "source_clause_parser_stage_output_id": self.clause_output.pk,
                "internal_usable_for_lookup": True,
                "parsed_event_cuts": [
                    {
                        "event_cut_id": "eventcut-good",
                        "doc_id": self.document.doc_id,
                        "clause_id": "003",
                        "event_cut_text": "was borowed of the sayde ladye laxston",
                        "trigger": "was borowed",
                        "lookup_context_text": "",
                        "ambiguity_context_note": "",
                        "clause_text": CLAUSE_3,
                        "clause_text_sha256": "hash",
                        "clause_sequence": 3,
                        "source_offsets": {"start": 58, "end": 98},
                        "source_clause_parser_stage_output_id": self.clause_output.pk,
                        "valid": True,
                    },
                    {
                        "event_cut_id": "eventcut-bad",
                        "doc_id": self.document.doc_id,
                        "clause_id": "003",
                        "event_cut_text": "was borrowed",
                        "valid": False,
                    },
                ],
            },
        )
        package = build_downstream_package(stage_output)
        self.assertEqual(package["contract_version"], DOWNSTREAM_CONTRACT_VERSION)
        self.assertEqual(package["event_cut_count"], 1)
        self.assertEqual(package["event_cuts"][0]["event_cut_id"], "eventcut-good")

        with TemporaryDirectory() as directory:
            output = Path(directory) / "eventcuts.json"
            call_command(
                "eventcut_export_downstream",
                eventcut_stage_output_id=stage_output.pk,
                output=output,
            )
            written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["event_cut_count"], 1)
