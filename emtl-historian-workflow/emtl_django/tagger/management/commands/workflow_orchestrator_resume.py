from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from tagger.models import Document
from tagger.services.workflow_orchestrator import build_orchestration_plan, resume_orchestration


class Command(BaseCommand):
    help = "Build and persist a dependency-closed, resumable workflow plan."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--stage", action="append", required=True)
        parser.add_argument("--no-persist", action="store_true")

    def handle(self, *args, **options) -> None:
        try:
            document = Document.objects.get(pk=options["document_id"])
            requested = set(options["stage"])
            plan = (
                build_orchestration_plan(document=document, requested_stages=requested)
                if options["no_persist"]
                else resume_orchestration(document=document, requested_stages=requested)
            )
        except (Document.DoesNotExist, DatabaseError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
