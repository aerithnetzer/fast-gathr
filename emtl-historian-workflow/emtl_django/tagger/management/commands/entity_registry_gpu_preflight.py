from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tagger.models import Document, StageOutput
from tagger.services.entity_gpu_preflight import EntityGpuPreflightRunner
from tagger.services.entity_knowledge import (
    DEFAULT_CANDIDATE_TOKEN_BUDGET,
    EntityKnowledgeRetrievalOptions,
    EntityKnowledgeRetriever,
)


class Command(BaseCommand):
    help = (
        "Build the source-complete Entity GPU payload and optionally call only the "
        "remote tokenizer endpoint. Generation is unavailable in this command."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--doc-id", required=True)
        parser.add_argument(
            "--candidate-token-budget",
            type=int,
            default=DEFAULT_CANDIDATE_TOKEN_BUDGET,
        )
        parser.add_argument(
            "--disable-fuzzy",
            action="store_true",
            help="Disable fuzzy candidate supplementation; exact/normalized remain active.",
        )
        parser.add_argument(
            "--remote-tokenizer",
            action="store_true",
            help="Call /tokenize only. This never calls /generate.",
        )

    def handle(self, *args, **options) -> None:
        document = Document.objects.filter(doc_id=str(options["doc_id"])).first()
        if document is None:
            raise CommandError(f"Document not found: {options['doc_id']}")
        stage_outputs = {
            output.stage: output
            for output in StageOutput.objects.filter(document=document)
        }
        retriever = EntityKnowledgeRetriever(
            options=EntityKnowledgeRetrievalOptions(
                candidate_token_budget=max(1, int(options["candidate_token_budget"])),
                enable_fuzzy_candidates=not bool(options["disable_fuzzy"]),
            )
        )
        runner = EntityGpuPreflightRunner(retriever=retriever)
        plan = runner.build_plan(document=document, stage_outputs=stage_outputs)
        report = dict(plan.report)
        if options["remote_tokenizer"]:
            tokenization = runner.tokenize_only(plan)
            report["provider_called"] = True
            report["tokenization_result"] = {
                "status": tokenization.status,
                "provider": tokenization.provider,
                "model": tokenization.model,
                "payload": tokenization.payload,
                "validation": tokenization.validation,
                "provenance": tokenization.provenance,
                "warnings": tokenization.warnings,
                "errors": tokenization.errors,
                "error": tokenization.error,
            }
            report["tokenizer_component_comparison"] = tokenization.payload.get(
                "component_comparison", {}
            )
            report["token_budget"] = tokenization.payload.get("token_counts", {})
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
