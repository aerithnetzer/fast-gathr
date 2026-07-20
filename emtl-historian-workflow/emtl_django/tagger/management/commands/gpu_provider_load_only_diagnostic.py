from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tagger.services.gpu_readiness import (
    LOAD_ONLY_OPERATION,
    readiness_summary,
    run_readiness_diagnostic,
)


class Command(BaseCommand):
    help = "Load the configured GPU model and run readiness gates without generation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--prompt-tokens", type=int, required=True)
        parser.add_argument("--request-id", default="")

    def handle(self, *args, **options) -> None:
        result = run_readiness_diagnostic(
            operation=LOAD_ONLY_OPERATION,
            prompt_tokens=int(options["prompt_tokens"]),
            request_id=str(options["request_id"] or ""),
        )
        self.stdout.write(json.dumps(readiness_summary(result), ensure_ascii=False, indent=2))
        if not result.passed:
            raise CommandError("Load-only readiness failed; do not run Entity generation.")
