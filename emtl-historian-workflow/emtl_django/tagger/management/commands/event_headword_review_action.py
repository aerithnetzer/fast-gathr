from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.services.event_headword_review import (
    ACTION_SUBMIT_PROPOSAL,
    EvidenceProposalSimilarityBackend,
    JsonReviewRepository,
    ReviewWorkflowError,
    accept_remaining_items,
    apply_review_action,
    create_review_item,
)


class Command(BaseCommand):
    help = "Initialize or mutate an Event headword review item without UI."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--store", type=Path, required=True)
        parser.add_argument("--initialize-from", type=Path)
        parser.add_argument("--overwrite", action="store_true")
        parser.add_argument("--item-id", default="")
        parser.add_argument("--action", default="")
        parser.add_argument("--actor", default="")
        parser.add_argument("--expected-revision", type=int)
        parser.add_argument("--action-id", default="")
        parser.add_argument("--candidate-rank", type=int)
        parser.add_argument("--proposed-headword", default="")
        parser.add_argument("--definition-hint", default="")
        parser.add_argument("--reviewer-note", default="")
        parser.add_argument("--similarity-evidence", type=Path)
        parser.add_argument("--similarity-top-k", type=int, default=10)
        parser.add_argument("--accept-remaining", action="store_true")

    def handle(self, *args, **options) -> None:
        repository = JsonReviewRepository(options["store"])
        try:
            if options["initialize_from"]:
                self._initialize(repository, options)
                return
            if options["accept_remaining"]:
                items, summary = accept_remaining_items(
                    repository.all(), actor=options["actor"] or "historian"
                )
                repository.replace_all(items)
                self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
                return
            self._apply(repository, options)
        except (ReviewWorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", "event_headword_review_failed")
            raise CommandError(f"{code}: {exc}") from exc

    def _initialize(self, repository: JsonReviewRepository, options: dict) -> None:
        if options["action"] or options["item_id"]:
            raise ReviewWorkflowError(
                "initialize_arguments_invalid",
                "--initialize-from cannot be combined with --action or --item-id",
            )
        source = json.loads(options["initialize_from"].read_text(encoding="utf-8"))
        item = create_review_item(
            item_id=source["item_id"],
            document_id=source["document_id"],
            clause_id=source["clause_id"],
            event_cut_id=source["event_cut_id"],
            event_cut_text=source["event_cut_text"],
            candidates=source["candidates"],
            chooser_output=source["chooser_output"],
            authority_version=source.get("authority_version", ""),
        )
        repository.initialize(item, overwrite=options["overwrite"])
        self.stdout.write(json.dumps(item, ensure_ascii=False, indent=2))

    def _apply(self, repository: JsonReviewRepository, options: dict) -> None:
        for name in ("item_id", "action", "actor"):
            if not options[name]:
                raise ReviewWorkflowError(
                    f"{name}_required", f"--{name.replace('_', '-')} is required"
                )
        if options["expected_revision"] is None:
            raise ReviewWorkflowError(
                "expected_revision_required", "--expected-revision is required"
            )
        similarity_backend = None
        if options["action"] == ACTION_SUBMIT_PROPOSAL:
            path = options["similarity_evidence"]
            if path is None:
                raise ReviewWorkflowError(
                    "encoder_similarity_required",
                    "--similarity-evidence from an encoder run is required",
                )
            evidence = json.loads(path.read_text(encoding="utf-8"))
            similarity_backend = EvidenceProposalSimilarityBackend(evidence)

        current = repository.get(options["item_id"])
        updated = apply_review_action(
            current,
            action=options["action"],
            actor=options["actor"],
            expected_revision=options["expected_revision"],
            action_id=options["action_id"] or None,
            candidate_rank=options["candidate_rank"],
            proposed_headword=options["proposed_headword"],
            definition_hint=options["definition_hint"],
            reviewer_note=options["reviewer_note"],
            similarity_backend=similarity_backend,
            similarity_top_k=max(1, options["similarity_top_k"]),
        )
        if updated is not current and updated != current:
            repository.save(updated, expected_revision=options["expected_revision"])
        self.stdout.write(json.dumps(updated, ensure_ascii=False, indent=2))
