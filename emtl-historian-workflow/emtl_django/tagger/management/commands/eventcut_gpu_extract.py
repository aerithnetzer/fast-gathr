from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.services.eventcut_extraction import (
    EventCutExtractionError,
    EventCutExtractionRunner,
    clause_records_from_output,
    resolve_accepted_clause_output,
    write_json_package,
)


class Command(BaseCommand):
    help = (
        "Extract internal EventCuts from accepted Clause Parser output using the "
        "real GPU-local provider. This stage has no user review transition."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--doc-id", default="")
        parser.add_argument("--clause-stage-output-id", type=int)
        parser.add_argument(
            "--clause-ids",
            action="append",
            default=[],
            help="Optional comma-separated Clause IDs; may be repeated.",
        )
        parser.add_argument("--confirm-real-generation", action="store_true")
        parser.add_argument("--request-id", default="")
        parser.add_argument("--output", type=Path)

    def handle(self, *args, **options) -> None:
        if not options["confirm_real_generation"]:
            raise CommandError(
                "Refusing EventCut extraction without --confirm-real-generation."
            )
        try:
            clause_output = resolve_accepted_clause_output(
                doc_id=options["doc_id"],
                clause_stage_output_id=options["clause_stage_output_id"],
            )
            clauses = clause_records_from_output(
                clause_output, options.get("clause_ids") or []
            )
            summary, package = EventCutExtractionRunner().run(
                clause_output=clause_output,
                clauses=clauses,
                confirm_real_generation=True,
                request_id=options["request_id"],
            )
            if options["output"]:
                write_json_package(package, options["output"])
                summary["output"] = str(options["output"])
        except EventCutExtractionError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        if not summary["internal_usable_for_lookup"]:
            raise CommandError(
                "EventCut extraction was recorded but is not usable for lookup; inspect "
                f"attempt {summary['attempt_id']}."
            )
