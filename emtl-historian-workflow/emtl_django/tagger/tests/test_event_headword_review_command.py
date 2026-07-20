from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class EventHeadwordReviewCommandTests(SimpleTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = self.root / "review.json"
        self.source = self.root / "source.json"
        self.source.write_text(
            json.dumps(
                {
                    "item_id": "item-1",
                    "document_id": "doc-1",
                    "clause_id": "001",
                    "event_cut_id": "cut-1",
                    "event_cut_text": "gave money",
                    "authority_version": "v1",
                    "candidates": [
                        {
                            "rank": 1,
                            "event_id": "E-0001",
                            "headword": "Give",
                            "score": 0.9,
                        }
                    ],
                    "chooser_output": {
                        "decision": "choose_candidate",
                        "selected_candidate": {
                            "rank": 1,
                            "event_id": "E-0001",
                            "headword": "Give",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_initialize_and_accept(self) -> None:
        call_command(
            "event_headword_review_action",
            store=self.store,
            initialize_from=self.source,
            stdout=StringIO(),
        )
        output = StringIO()
        call_command(
            "event_headword_review_action",
            store=self.store,
            item_id="item-1",
            action="accept",
            actor="historian-1",
            expected_revision=0,
            stdout=output,
        )
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "accepted_existing_headword")
        self.assertEqual(result["assignment"]["event_id"], "E-0001")

    def test_submit_proposal_refuses_missing_similarity_evidence(self) -> None:
        call_command(
            "event_headword_review_action",
            store=self.store,
            initialize_from=self.source,
            stdout=StringIO(),
        )
        call_command(
            "event_headword_review_action",
            store=self.store,
            item_id="item-1",
            action="reject",
            actor="historian-1",
            expected_revision=0,
            stdout=StringIO(),
        )
        with self.assertRaisesRegex(CommandError, "encoder_similarity_required"):
            call_command(
                "event_headword_review_action",
                store=self.store,
                item_id="item-1",
                action="submit_proposal",
                actor="historian-1",
                expected_revision=1,
                proposed_headword="Gives",
                stdout=StringIO(),
            )
