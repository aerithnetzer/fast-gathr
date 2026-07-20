from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from django.test import SimpleTestCase

from tagger.services.event_headword_review import (
    ACTION_ACCEPT,
    ACTION_CHOOSE_SIMILAR,
    ACTION_CONFIRM_PROPOSAL,
    ACTION_EDIT,
    ACTION_REJECT,
    ACTION_SUBMIT_PROPOSAL,
    CONTRACT_VERSION,
    STATE_ACCEPTED,
    STATE_EDITING,
    STATE_PROVISIONAL,
    STATE_SIMILARITY_REVIEW,
    AuthorityHeadword,
    DenseProposalSimilarityBackend,
    JsonReviewRepository,
    ReviewWorkflowError,
    RevisionConflictError,
    apply_review_action,
    accept_remaining_items,
    create_review_item,
)


def candidates() -> list[dict]:
    return [
        {"rank": 1, "event_id": "E-0001", "headword": "Give", "score": 0.9},
        {"rank": 2, "event_id": "E-0002", "headword": "Deliver", "score": 0.8},
    ]


def selected_item() -> dict:
    return create_review_item(
        item_id="item-1",
        document_id="doc-1",
        clause_id="001",
        event_cut_id="cut-1",
        event_cut_text="gave the money",
        candidates=candidates(),
        chooser_output={
            "decision": "choose_candidate",
            "selected_candidate": {
                "rank": 1,
                "event_id": "E-0001",
                "headword": "Give",
            },
        },
        authority_version="v1",
    )


def none_fit_item() -> dict:
    return create_review_item(
        item_id="item-2",
        document_id="doc-1",
        clause_id="002",
        event_cut_id="cut-2",
        event_cut_text="unknown action",
        candidates=candidates(),
        chooser_output={"decision": "none_of_these_fit", "selected_candidate": None},
    )


class FakeEncoder:
    def encode_documents(self, texts):
        return np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    def encode_queries(self, texts):
        return np.asarray([[0.9, 0.1]], dtype=np.float32)


def similarity_backend() -> DenseProposalSimilarityBackend:
    return DenseProposalSimilarityBackend(
        authority=[
            AuthorityHeadword("E-0001", "Give", "transfer", "did give", "v1"),
            AuthorityHeadword("E-0002", "Deliver", "hand over", "delivered", "v1"),
        ],
        encoder=FakeEncoder(),
        model_name="test-encoder",
        authority_hash="authority-hash",
    )


class EventHeadwordReviewWorkflowTests(SimpleTestCase):
    def test_none_fit_shows_accept_and_reject_as_disabled(self) -> None:
        item = none_fit_item()
        primary = item["allowed_actions"]["primary"]
        self.assertFalse(primary[ACTION_ACCEPT]["enabled"])
        self.assertTrue(primary[ACTION_EDIT]["enabled"])
        self.assertFalse(primary[ACTION_REJECT]["enabled"])
        self.assertEqual(primary[ACTION_ACCEPT]["cursor"], "not-allowed")

    def test_accept_persists_stable_event_assignment(self) -> None:
        item = apply_review_action(
            selected_item(), action=ACTION_ACCEPT, actor="historian-1", expected_revision=0
        )
        self.assertEqual(item["state"], STATE_ACCEPTED)
        self.assertEqual(item["assignment"]["event_cut_id"], "cut-1")
        self.assertEqual(item["assignment"]["event_id"], "E-0001")
        self.assertEqual(item["assignment"]["authority_version"], "v1")

    def test_edit_prefills_llm_choice_and_reject_starts_blank(self) -> None:
        edited = apply_review_action(
            selected_item(), action=ACTION_EDIT, actor="historian-1", expected_revision=0
        )
        rejected = apply_review_action(
            selected_item(), action=ACTION_REJECT, actor="historian-1", expected_revision=0
        )
        self.assertEqual(edited["state"], STATE_EDITING)
        self.assertEqual(edited["edit_context"]["prefill_headword"], "Give")
        self.assertEqual(edited["edit_context"]["origin"], "edit")
        self.assertEqual(rejected["edit_context"]["prefill_headword"], "")
        self.assertEqual(rejected["edit_context"]["origin"], "reject")

    def test_none_fit_edit_starts_blank(self) -> None:
        item = apply_review_action(
            none_fit_item(), action=ACTION_EDIT, actor="historian-1", expected_revision=0
        )
        self.assertEqual(item["state"], STATE_EDITING)
        self.assertTrue(item["edit_context"]["input_is_blank"])

    def test_proposal_requires_encoder_similarity(self) -> None:
        item = apply_review_action(
            selected_item(), action=ACTION_REJECT, actor="historian-1", expected_revision=0
        )
        with self.assertRaisesRegex(ReviewWorkflowError, "encoder similarity"):
            apply_review_action(
                item,
                action=ACTION_SUBMIT_PROPOSAL,
                actor="historian-1",
                expected_revision=1,
                proposed_headword="Gives",
            )

    def test_proposal_can_choose_existing_or_force_provisional(self) -> None:
        editing = apply_review_action(
            selected_item(), action=ACTION_REJECT, actor="historian-1", expected_revision=0
        )
        checked = apply_review_action(
            editing,
            action=ACTION_SUBMIT_PROPOSAL,
            actor="historian-1",
            expected_revision=1,
            proposed_headword="Gives",
            definition_hint="transfer something",
            similarity_backend=similarity_backend(),
        )
        self.assertEqual(checked["state"], STATE_SIMILARITY_REVIEW)
        self.assertEqual(checked["similarity_check"]["matches"][0]["event_id"], "E-0001")

        existing = apply_review_action(
            checked,
            action=ACTION_CHOOSE_SIMILAR,
            actor="historian-1",
            expected_revision=2,
            candidate_rank=1,
        )
        self.assertEqual(existing["state"], STATE_ACCEPTED)
        self.assertEqual(existing["assignment"]["event_id"], "E-0001")

        provisional = apply_review_action(
            checked,
            action=ACTION_CONFIRM_PROPOSAL,
            actor="historian-1",
            expected_revision=2,
        )
        self.assertEqual(provisional["state"], STATE_PROVISIONAL)
        self.assertEqual(
            provisional["provisional_headword"]["status"],
            "pending_shared_vocabulary_review",
        )
        self.assertFalse(
            provisional["provisional_headword"]["official_event_list_modified"]
        )

    def test_revision_conflict_and_duplicate_action_are_rejected(self) -> None:
        with self.assertRaises(RevisionConflictError):
            apply_review_action(
                selected_item(), action=ACTION_ACCEPT, actor="historian-1", expected_revision=2
            )
        item = apply_review_action(
            selected_item(),
            action=ACTION_EDIT,
            actor="historian-1",
            expected_revision=0,
            action_id="action-1",
        )
        with self.assertRaisesRegex(ReviewWorkflowError, "already applied"):
            apply_review_action(
                item,
                action=ACTION_SUBMIT_PROPOSAL,
                actor="historian-1",
                expected_revision=1,
                action_id="action-1",
                proposed_headword="Give",
                similarity_backend=similarity_backend(),
            )

    def test_json_repository_uses_revision_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonReviewRepository(Path(directory) / "review.json")
            repository.initialize(selected_item())
            item = repository.get("item-1")
            updated = apply_review_action(
                item, action=ACTION_ACCEPT, actor="historian-1", expected_revision=0
            )
            repository.save(updated, expected_revision=0)
            self.assertEqual(repository.get("item-1")["state"], STATE_ACCEPTED)
            with self.assertRaises(RevisionConflictError):
                repository.save(updated, expected_revision=0)

    def test_contract_is_explicit(self) -> None:
        self.assertEqual(selected_item()["contract_version"], CONTRACT_VERSION)

    def test_accept_remaining_preserves_edited_and_none_fit_items(self) -> None:
        edited = apply_review_action(
            selected_item(), action=ACTION_EDIT, actor="historian-1", expected_revision=0
        )
        remaining = selected_item()
        remaining["item_id"] = "item-remaining"
        updated, summary = accept_remaining_items(
            [edited, none_fit_item(), remaining], actor="historian-1", action_id_prefix="bulk-1"
        )
        self.assertEqual(summary, {"accepted": 1, "preserved": 2, "total": 3})
        self.assertEqual(updated[0]["state"], STATE_EDITING)
        self.assertEqual(updated[1]["state"], "llm_none_fit")
        self.assertEqual(updated[2]["state"], STATE_ACCEPTED)
