from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tagger.models import Document, StageExecutionAttempt, StageOutput
from tagger.services.summary_assembler_generation import (
    AssemblerGenerationService,
    SummaryAssemblerError,
    SummaryKeywordsGenerationService,
)


class Command(BaseCommand):
    help = "Run Summary & Keywords and a bounded Assembler Occurrence-conservation pilot."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--clause-stage-output-id", type=int, required=True)
        parser.add_argument("--entity-stage-output-id", type=int, required=True)
        parser.add_argument("--occurrence-stage-output-id", type=int, required=True)
        parser.add_argument("--request-prefix", required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options) -> None:
        try:
            document = Document.objects.get(pk=options["document_id"])
            clause = StageOutput.objects.select_related("document").get(pk=options["clause_stage_output_id"])
            entity = StageOutput.objects.select_related("document").get(pk=options["entity_stage_output_id"])
            occurrence = StageOutput.objects.select_related("document").get(pk=options["occurrence_stage_output_id"])
            prefix = options["request_prefix"]
            summary = SummaryKeywordsGenerationService().run(
                document=document, request_id=f"{prefix}-summary"
            )
            self._persist(document, StageOutput.Stage.SUMMARY_KEYWORDS, "Summary & Keywords", summary)
            assembler = AssemblerGenerationService().run(
                clause_output=clause,
                entity_output=entity,
                occurrence_output=occurrence,
                request_id=f"{prefix}-assembler",
                conservation_test=True,
            )
            self._persist(document, StageOutput.Stage.TAG_ASSEMBLER, "Tag Assembler", assembler)
        except (Document.DoesNotExist, StageOutput.DoesNotExist, SummaryAssemblerError) as exc:
            raise CommandError(str(exc)) from exc
        package = {
            "summary_keywords": _as_dict(summary),
            "tag_assembler": _as_dict(assembler),
            "acceptance": {
                "summary_keywords_valid": summary.validation.get("valid", False),
                "assembler_occurrence_conserved": assembler.validation.get("valid", False),
                "occurrence_input_status": occurrence.status,
                "occurrence_input_was_accepted": occurrence.status == StageOutput.Status.ACCEPTED,
            },
        }
        options["output"].parent.mkdir(parents=True, exist_ok=True)
        options["output"].write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(package["acceptance"], indent=2))

    @transaction.atomic
    def _persist(self, document, stage, title, result) -> None:
        stage_output, _ = StageOutput.objects.select_for_update().get_or_create(
            document=document, stage=stage, defaults={"display_title": title}
        )
        protected = stage_output.status == StageOutput.Status.ACCEPTED
        disposition = (
            StageExecutionAttempt.Disposition.ACCEPTED_PROTECTED
            if protected
            else StageExecutionAttempt.Disposition.APPLIED_TO_CHECKING
            if result.raw_output
            else StageExecutionAttempt.Disposition.INVALID_NOT_APPLIED
        )
        attempt = StageExecutionAttempt.objects.create(
            stage_output=stage_output,
            request_id=(result.request or {}).get("request_id"),
            stage=stage,
            execution_status=result.status,
            disposition=disposition,
            provider=result.provider,
            model=result.model,
            raw_output=result.raw_output,
            payload=result.payload,
            provenance=result.provenance,
            validation=result.validation,
            error=result.error,
            applied_to_stage_output=bool(result.raw_output and not protected),
        )
        if result.raw_output and not protected:
            stage_output.status = StageOutput.Status.CHECKING
            stage_output.raw_output = result.raw_output
            stage_output.payload = result.payload
            stage_output.provenance = {**result.provenance, "attempt_id": attempt.pk}
            stage_output.save(update_fields=["status", "raw_output", "payload", "provenance", "updated_at"])


def _as_dict(result) -> dict:
    return {
        "status": result.status,
        "raw_output": result.raw_output,
        "payload": result.payload,
        "provenance": result.provenance,
        "validation": result.validation,
        "error": result.error,
    }
