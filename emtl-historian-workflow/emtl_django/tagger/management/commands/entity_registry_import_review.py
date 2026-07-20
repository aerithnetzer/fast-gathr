import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.models import StageOutput
from tagger.services.entity_review_handoff import import_entity_review_file


class Command(BaseCommand):
    help = "Import reviewed Entity JSON and explicitly approve it for downstream use."

    def add_arguments(self, parser):
        parser.add_argument("--stage-output-id", type=int, required=True)
        parser.add_argument("--input", type=Path, required=True)
        parser.add_argument("--confirm-approve-for-downstream", action="store_true")
        parser.add_argument("--accept-remaining", action="store_true")

    def handle(self, *args, **options):
        try:
            stage_output = StageOutput.objects.get(pk=options["stage_output_id"])
            package = import_entity_review_file(
                stage_output=stage_output,
                input_path=options["input"],
                confirm_approve_for_downstream=options["confirm_approve_for_downstream"],
                accept_remaining=options["accept_remaining"],
            )
        except (StageOutput.DoesNotExist, ValueError, json.JSONDecodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"stage_output_id={package['stage_output_id']} state=accepted "
            f"downstream_rows={len(package['reviewed_rows'])}"
        ))
