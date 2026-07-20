from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tagger.models import StageExecutionAttempt, StageOutput
from tagger.services.occurrence_generation import (
    OccurrenceGenerationError,
    OccurrenceGenerationService,
)


class Command(BaseCommand):
    help = "Run bounded real-GPU Occurrence generation from accepted Clause and reviewed Entity outputs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--clause-stage-output-id", type=int, required=True)
        parser.add_argument("--entity-stage-output-id", type=int, required=True)
        parser.add_argument("--clause-ids", action="append", required=True)
        parser.add_argument("--eventcut-stage-output-id", type=int, required=True)
        parser.add_argument("--event-review-store", type=Path, required=True)
        parser.add_argument("--request-id", required=True)
        parser.add_argument("--max-output-tokens", type=int, default=2048)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options) -> None:
        try:
            clause_output = StageOutput.objects.select_related("document").get(
                pk=options["clause_stage_output_id"]
            )
            entity_output = StageOutput.objects.select_related("document").get(
                pk=options["entity_stage_output_id"]
            )
            eventcut_output = StageOutput.objects.select_related("document").get(
                pk=options["eventcut_stage_output_id"]
            )
            result = OccurrenceGenerationService().run(
                clause_output=clause_output,
                entity_output=entity_output,
                eventcut_output=eventcut_output,
                clause_ids=options["clause_ids"],
                event_review_store_path=options["event_review_store"],
                request_id=options["request_id"],
                max_output_tokens=max(256, options["max_output_tokens"]),
            )
            self._persist(clause_output, result)
            package = {
                "status": result.status,
                "raw_output": result.raw_output,
                "payload": result.payload,
                "provenance": result.provenance,
                "validation": result.validation,
                "error": result.error,
            }
            options["output"].parent.mkdir(parents=True, exist_ok=True)
            options["output"].write_text(
                json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (StageOutput.DoesNotExist, OccurrenceGenerationError, ValueError) as exc:
            code = getattr(exc, "code", "occurrence_generation_failed")
            raise CommandError(f"{code}: {exc}") from exc
        self.stdout.write(
            json.dumps(
                {
                    "status": result.status,
                    "model": result.model,
                    "model_call_completed": result.provenance.get("model_call_completed", False),
                    "validation_valid": result.validation.get("valid", False),
                    "tag_count": result.validation.get("tag_count", 0),
                    "output": str(options["output"]),
                },
                indent=2,
            )
        )

    @transaction.atomic
    def _persist(self, clause_output: StageOutput, result) -> None:
        occurrence, _ = StageOutput.objects.select_for_update().get_or_create(
            document=clause_output.document,
            stage=StageOutput.Stage.OCCURRENCES_REGISTRY,
            defaults={"display_title": "Occurrences Registry"},
        )
        attempt = StageExecutionAttempt.objects.create(
            stage_output=occurrence,
            request_id=(result.request or {}).get("request_id"),
            stage=StageOutput.Stage.OCCURRENCES_REGISTRY,
            execution_status=result.status,
            disposition=(
                StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING
                if result.raw_output
                else StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED
            ),
            provider=result.provider,
            model=result.model,
            raw_output=result.raw_output,
            payload=result.payload,
            provenance=result.provenance,
            validation=result.validation,
            error=result.error,
            applied_to_stage_output=bool(result.raw_output),
        )
        if result.raw_output:
            occurrence.status = StageOutput.Status.CHECKING
            occurrence.raw_output = result.raw_output
            occurrence.payload = result.payload
            occurrence.provenance = {
                **result.provenance,
                "attempt_id": attempt.pk,
                "approved_for_downstream": False,
            }
            occurrence.save(
                update_fields=["status", "raw_output", "payload", "provenance", "updated_at"]
            )
