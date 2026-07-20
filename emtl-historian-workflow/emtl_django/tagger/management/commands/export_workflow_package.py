import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.models import Document
from tagger.services.export_contract import write_workflow_export


class Command(BaseCommand):
    help = "Export one document using the formal EMTL workflow handoff contract."

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        try:
            document = Document.objects.get(pk=options["document_id"])
            package = write_workflow_export(document=document, output_path=options["output"])
        except (Document.DoesNotExist, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "schema_version": package["schema_version"],
            "export_id": package["export_id"],
            "accepted_stage_ids": package["integrity"]["accepted_stage_ids"],
            "audit_only_stage_ids": package["integrity"]["nonaccepted_stage_ids_audit_only"],
            "output": str(options["output"]),
        }, indent=2))
