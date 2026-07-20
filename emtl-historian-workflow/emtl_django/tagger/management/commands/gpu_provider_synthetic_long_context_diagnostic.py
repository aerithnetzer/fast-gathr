from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tagger.services.gpu_readiness import (
    SYNTHETIC_OPERATION,
    readiness_summary,
    run_readiness_diagnostic,
)


class Command(BaseCommand):
    help = (
        "Run a one-token, provider-generated synthetic long-context diagnostic. "
        "No business document or registry resource is sent."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--prompt-tokens", type=int, required=True)
        parser.add_argument("--request-id", default="")
        parser.add_argument("--confirm-synthetic-diagnostic", action="store_true")

    def handle(self, *args, **options) -> None:
        if not options["confirm_synthetic_diagnostic"]:
            raise CommandError(
                "Refusing diagnostic model call without --confirm-synthetic-diagnostic."
            )
        result = run_readiness_diagnostic(
            operation=SYNTHETIC_OPERATION,
            prompt_tokens=int(options["prompt_tokens"]),
            request_id=str(options["request_id"] or ""),
        )
        self.stdout.write(json.dumps(readiness_summary(result), ensure_ascii=False, indent=2))
        if not result.passed:
            raise CommandError("Synthetic readiness failed; do not run Entity generation.")
