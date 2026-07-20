from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.models import StageOutput
from tagger.services.eventcut_extraction import (
    EventCutExtractionError,
    build_downstream_package,
    write_json_package,
)


class Command(BaseCommand):
    help = "Export validated internal EventCuts for downstream Event lookup."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--doc-id", default="")
        parser.add_argument("--eventcut-stage-output-id", type=int)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options) -> None:
        doc_id = str(options["doc_id"] or "").strip()
        stage_output_id = options["eventcut_stage_output_id"]
        if not doc_id and stage_output_id is None:
            raise CommandError(
                "Provide --doc-id and/or --eventcut-stage-output-id."
            )
        query = StageOutput.objects.select_related("document").filter(
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION
        )
        if doc_id:
            query = query.filter(document__doc_id=doc_id)
        if stage_output_id is not None:
            query = query.filter(pk=stage_output_id)
        stage_output = query.first()
        if stage_output is None:
            raise CommandError("Matching EventCut StageOutput was not found.")
        try:
            package = build_downstream_package(stage_output)
            write_json_package(package, options["output"])
        except EventCutExtractionError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(
            json.dumps(
                {
                    "contract_version": package["contract_version"],
                    "package_id": package["package_id"],
                    "event_cut_count": package["event_cut_count"],
                    "output": str(options["output"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
