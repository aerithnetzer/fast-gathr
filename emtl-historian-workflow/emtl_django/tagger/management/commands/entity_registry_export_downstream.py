from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.models import StageOutput
from tagger.services.entity_review_handoff import write_entity_downstream_package


class Command(BaseCommand):
    help = "Export explicitly approved Entity rows for downstream Occurrence input."

    def add_arguments(self, parser):
        parser.add_argument("--stage-output-id", type=int, required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        try:
            stage_output = StageOutput.objects.get(pk=options["stage_output_id"])
            package = write_entity_downstream_package(stage_output=stage_output, output_path=options["output"])
        except (StageOutput.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"rows={len(package['reviewed_rows'])} output={options['output']}"))
