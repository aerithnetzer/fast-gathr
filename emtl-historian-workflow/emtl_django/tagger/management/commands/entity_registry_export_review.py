from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.models import StageOutput
from tagger.services.entity_review_handoff import export_entity_review_file


class Command(BaseCommand):
    help = "Export a checking Entity review candidate as editable JSON."

    def add_arguments(self, parser):
        parser.add_argument("--stage-output-id", type=int, required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        try:
            stage_output = StageOutput.objects.get(pk=options["stage_output_id"])
            document = export_entity_review_file(stage_output=stage_output, output_path=options["output"])
        except (StageOutput.DoesNotExist, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"rows={len(document['rows'])} output={options['output']}"))
