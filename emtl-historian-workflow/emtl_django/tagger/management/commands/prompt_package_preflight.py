from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tagger.models import Document, StageOutput
from tagger.services.prompt_preflight import (
    DEFAULT_PREFLIGHT_STAGES,
    PROMPT_BUDGETS,
    RUNTIME_MODES,
    PromptPreflightOptions,
    build_prompt_package_preflight_report,
    synthetic_preflight_document,
)


class Command(BaseCommand):
    help = "Build prompt/provider payload preflight reports without calling any model."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--stages",
            default=",".join(DEFAULT_PREFLIGHT_STAGES),
            help="Comma-separated stage ids to inspect.",
        )
        parser.add_argument(
            "--budget",
            choices=sorted(PROMPT_BUDGETS),
            default="compact",
            help="Named prompt budget.",
        )
        parser.add_argument(
            "--budget-chars",
            type=int,
            default=None,
            help="Override named budget with an explicit character limit.",
        )
        parser.add_argument(
            "--provider",
            default="gpu_local",
            help="Provider label to use in the provider payload shape.",
        )
        parser.add_argument(
            "--runtime-mode",
            choices=RUNTIME_MODES,
            default="compact_diagnostic",
            help=(
                "Preflight posture: compact_diagnostic reports source issues; "
                "source_complete_readiness treats missing/truncated/omitted resources as blockers."
            ),
        )
        parser.add_argument(
            "--doc-id",
            default="",
            help="Use an existing Django Document and its current StageOutput rows.",
        )
        parser.add_argument(
            "--text",
            default="",
            help="Synthetic document text when --doc-id is not supplied.",
        )

    def handle(self, *args, **options) -> None:
        stage_ids = [
            item.strip()
            for item in str(options["stages"] or "").split(",")
            if item.strip()
        ]
        if not stage_ids:
            raise CommandError("At least one stage id is required.")
        document, stage_outputs = _document_and_stage_outputs(
            doc_id=str(options["doc_id"] or "").strip(),
            text=str(options["text"] or ""),
        )
        report = build_prompt_package_preflight_report(
            document=document,
            stage_outputs=stage_outputs,
            stage_ids=stage_ids,
            options=PromptPreflightOptions(
                budget_name=str(options["budget"]),
                budget_chars=options["budget_chars"],
                provider=str(options["provider"]),
                runtime_mode=str(options["runtime_mode"]),
            ),
        )
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))


def _document_and_stage_outputs(*, doc_id: str, text: str) -> tuple[object, dict[str, StageOutput]]:
    if not doc_id:
        return synthetic_preflight_document(text), {}
    document = Document.objects.filter(doc_id=doc_id).first()
    if not document:
        raise CommandError(f"Document not found: {doc_id}")
    return document, {
        stage_output.stage: stage_output
        for stage_output in StageOutput.objects.filter(document=document)
    }
