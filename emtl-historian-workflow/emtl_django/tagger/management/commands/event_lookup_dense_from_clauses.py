from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.services.event_lookup_dense import (
    DEFAULT_ENCODER_MODEL,
    DenseEventLookupError,
    DenseEventLookupFromClausesService,
    write_dense_lookup_package,
)
from tagger.services.eventcut_extraction import (
    EventCutExtractionError,
    clause_records_from_output,
    resolve_accepted_clause_output,
)


class Command(BaseCommand):
    help = (
        "Run real dense cosine Event lookup from accepted Clause Parser output, "
        "using the latest validated internal EventCuts."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--doc-id", default="")
        parser.add_argument("--clause-stage-output-id", type=int)
        parser.add_argument(
            "--clause-ids",
            action="append",
            required=True,
            help="Comma-separated zero-padded Clause IDs; may be repeated.",
        )
        parser.add_argument("--top-k", type=int, default=20)
        parser.add_argument("--encoder-model", default=DEFAULT_ENCODER_MODEL)
        parser.add_argument("--encoder-device", default="")
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options) -> None:
        try:
            clause_output = resolve_accepted_clause_output(
                doc_id=options["doc_id"],
                clause_stage_output_id=options["clause_stage_output_id"],
            )
            clauses = clause_records_from_output(
                clause_output, options.get("clause_ids") or []
            )
            package = DenseEventLookupFromClausesService().build(
                clause_output=clause_output,
                clauses=clauses,
                top_k=options["top_k"],
                encoder_model=options["encoder_model"],
                encoder_device=options["encoder_device"],
            )
            write_dense_lookup_package(package, options["output"])
        except (DenseEventLookupError, EventCutExtractionError) as exc:
            code = getattr(exc, "code", "event_lookup_dense_failed")
            raise CommandError(f"{code}: {exc}") from exc
        self.stdout.write(
            json.dumps(
                {
                    "contract_version": package["contract_version"],
                    "doc_id": package["doc_id"],
                    "selected_clause_ids": package["selected_clause_ids"],
                    "event_cut_count": package["event_cut_count"],
                    "encoder_model": package["encoder_model"],
                    "encoder_device": package["encoder_device"],
                    "embedding_dim": package["embedding_dim"],
                    "backend_name": package["backend_name"],
                    "tfidf_used": False,
                    "output": str(options["output"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
