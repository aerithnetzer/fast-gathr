from __future__ import annotations

import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from django.core.management import call_command
from django.test import TestCase

from tagger.models import Document, StageOutput
from tagger.services.event_lookup_dense import (
    DENSE_BACKEND_NAME,
    DENSE_FROM_CLAUSES_CONTRACT,
    DENSE_SCORE_TYPE,
    DenseEventLookupError,
    DenseEventLookupFromClausesService,
)
from tagger.services.eventcut_extraction import (
    INTERNAL_CONTRACT_VERSION,
    clause_records_from_output,
)


TEXT_003 = "the money was borowed of the sayde ladye laxston"
TEXT_005 = "the parties shoulde enter into newe bandes"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeDenseEncoder:
    def __init__(self, model_name: str, device: str) -> None:
        self.model_name = model_name
        self.device = device or "cpu"

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            [
                [
                    float((sum(ord(char) for char in text) % 97) + 1),
                    float((len(text) % 31) + 1),
                    float((text.count(" ") % 17) + 1),
                ]
                for text in texts
            ],
            dtype=np.float32,
        )


class DenseEventLookupFromClausesTests(TestCase):
    def setUp(self) -> None:
        self.document = Document.objects.create(doc_id="dense-doc", title="Dense test")
        self.clause_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.ACCEPTED,
            display_title="Clause Parser",
            payload={
                "clauses": [
                    {"clause_id": "003", "sequence": 3, "text": TEXT_003},
                    {"clause_id": "005", "sequence": 5, "text": TEXT_005},
                ]
            },
        )
        self.eventcut_output = StageOutput.objects.create(
            document=self.document,
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION,
            status=StageOutput.Status.LOADED,
            display_title="EventCut Extraction (internal)",
            payload={
                "contract_version": INTERNAL_CONTRACT_VERSION,
                "source_clause_parser_stage_output_id": self.clause_output.pk,
                "internal_usable_for_lookup": True,
                "parsed_event_cuts": [
                    {
                        "event_cut_id": "eventcut-003",
                        "clause_id": "003",
                        "trigger": "was borowed",
                        "event_cut_text": "was borowed of the sayde ladye laxston",
                        "lookup_context_text": TEXT_003,
                        "clause_text_sha256": _sha(TEXT_003),
                        "source_clause_parser_stage_output_id": self.clause_output.pk,
                        "source_offsets": {"start": 10, "end": 49},
                        "valid": True,
                    },
                    {
                        "event_cut_id": "eventcut-005",
                        "clause_id": "005",
                        "trigger": "enter",
                        "event_cut_text": "shoulde enter into newe bandes",
                        "lookup_context_text": TEXT_005,
                        "clause_text_sha256": _sha(TEXT_005),
                        "source_clause_parser_stage_output_id": self.clause_output.pk,
                        "source_offsets": {"start": 12, "end": 43},
                        "valid": True,
                    },
                ],
            },
        )

    def service(self) -> DenseEventLookupFromClausesService:
        return DenseEventLookupFromClausesService(
            encoder_factory=lambda model, device: FakeDenseEncoder(model, device)
        )

    def test_dense_backend_returns_ranked_top_k_with_full_provenance(self) -> None:
        clauses = clause_records_from_output(self.clause_output, "003,005")
        package = self.service().build(
            clause_output=self.clause_output,
            clauses=clauses,
            top_k=3,
            encoder_model="test/dense-encoder",
            encoder_device="cpu",
        )
        self.assertEqual(package["contract_version"], DENSE_FROM_CLAUSES_CONTRACT)
        self.assertEqual(package["selected_clause_ids"], ["003", "005"])
        self.assertEqual(package["backend_name"], DENSE_BACKEND_NAME)
        self.assertEqual(package["score_type"], DENSE_SCORE_TYPE)
        self.assertFalse(package["tfidf_used"])
        self.assertFalse(package["lexical_fallback_used"])
        self.assertEqual(package["embedding_dim"], 3)
        self.assertIn("Chatbot docs/Events_List_VectorLLM_v1.xlsx", package["registry_path"])
        self.assertEqual(len(package["registry_hash"]), 64)
        self.assertEqual(len(package["index_hash"]), 64)
        self.assertEqual(package["source_eventcut_stage_output_id"], self.eventcut_output.pk)
        self.assertEqual([cut["clause_id"] for cut in package["event_cuts"]], ["003", "005"])
        for event_cut in package["event_cuts"]:
            self.assertEqual(len(event_cut["candidates"]), 3)
            self.assertEqual(
                [item["rank"] for item in event_cut["candidates"]], [1, 2, 3]
            )
            self.assertTrue(
                all(item["score_type"] == DENSE_SCORE_TYPE for item in event_cut["candidates"])
            )
            self.assertTrue(
                all(item["provenance"]["index_hash"] == package["index_hash"] for item in event_cut["candidates"])
            )

    def test_missing_validated_eventcuts_identifies_clause_ids(self) -> None:
        self.eventcut_output.payload["parsed_event_cuts"] = [
            self.eventcut_output.payload["parsed_event_cuts"][0]
        ]
        self.eventcut_output.save(update_fields=["payload"])
        clauses = clause_records_from_output(self.clause_output, ["003", "005"])
        with self.assertRaisesMessage(DenseEventLookupError, "005"):
            self.service().build(
                clause_output=self.clause_output,
                clauses=clauses,
                top_k=3,
                encoder_model="test/dense-encoder",
            )

    def test_command_accepts_clause_source_and_uses_internal_eventcuts(self) -> None:
        fake_package = {
            "contract_version": DENSE_FROM_CLAUSES_CONTRACT,
            "doc_id": self.document.doc_id,
            "selected_clause_ids": ["003", "005"],
            "event_cut_count": 2,
            "encoder_model": "test/dense-encoder",
            "encoder_device": "cpu",
            "embedding_dim": 3,
            "backend_name": DENSE_BACKEND_NAME,
            "tfidf_used": False,
        }
        with TemporaryDirectory() as directory, patch(
            "tagger.management.commands.event_lookup_dense_from_clauses.DenseEventLookupFromClausesService"
        ) as service_class, patch(
            "tagger.management.commands.event_lookup_dense_from_clauses.write_dense_lookup_package"
        ) as write_package:
            service_class.return_value.build.return_value = fake_package
            call_command(
                "event_lookup_dense_from_clauses",
                clause_stage_output_id=self.clause_output.pk,
                clause_ids="003,005",
                top_k=7,
                encoder_model="test/dense-encoder",
                output=Path(directory) / "dense.json",
                stdout=io.StringIO(),
            )
        build_kwargs = service_class.return_value.build.call_args.kwargs
        self.assertEqual(
            [item["clause_id"] for item in build_kwargs["clauses"]], ["003", "005"]
        )
        self.assertEqual(build_kwargs["clause_output"].pk, self.clause_output.pk)
        self.assertEqual(build_kwargs["top_k"], 7)
        write_package.assert_called_once()
