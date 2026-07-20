from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tagger.models import Document, StageOutput
from tagger.services.entity_generation import (
    EntityControlledGenerationError,
    EntityControlledGenerationRunner,
)


class Command(BaseCommand):
    help = (
        "Perform one explicitly confirmed, tokenizer-gated real Entity Registry "
        "generation. This command never retries and never accepts the result."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--doc-id", required=True)
        parser.add_argument(
            "--confirm-real-generation",
            action="store_true",
            help="Required acknowledgement that this command may call /generate exactly once.",
        )
        parser.add_argument(
            "--request-id",
            default="",
            help=(
                "Optional explicit unique request ID. Reusing an existing ID is rejected "
                "before any provider call."
            ),
        )

    def handle(self, *args, **options) -> None:
        if not bool(options["confirm_real_generation"]):
            raise CommandError(
                "Refusing Entity generation without --confirm-real-generation."
            )
        document = Document.objects.filter(doc_id=str(options["doc_id"])).first()
        if document is None:
            raise CommandError(f"Document not found: {options['doc_id']}")
        stage_output = StageOutput.objects.filter(
            document=document,
            stage=StageOutput.Stage.ENTITY_REGISTRY,
        ).first()
        if stage_output is None:
            raise CommandError(
                "Entity Registry StageOutput placeholder is missing; no provider call was made."
            )
        try:
            summary = EntityControlledGenerationRunner().run(
                document=document,
                stage_output=stage_output,
                confirm_real_generation=True,
                request_id=str(options["request_id"] or ""),
            )
        except EntityControlledGenerationError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        if not (
            summary.get("execution_status") == "completed"
            and summary.get("lifecycle_status") == StageOutput.Status.CHECKING
        ):
            raise CommandError(
                "Entity generation did not produce a valid checking result; inspect attempt "
                f"{summary.get('attempt_id')}."
            )
