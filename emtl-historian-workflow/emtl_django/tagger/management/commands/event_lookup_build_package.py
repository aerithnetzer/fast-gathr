from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tagger.services.event_lookup_contract import (
    EventCutInput,
    EventLookupPackageBuilder,
    write_event_lookup_package,
)


class Command(BaseCommand):
    help = "Build a model-free EventCut candidate package; TF-IDF is forbidden."

    def add_arguments(self, parser):
        parser.add_argument("--event-cut-text", required=True)
        parser.add_argument("--event-cut-id", default="event-cut-command-001")
        parser.add_argument("--trigger", default="")
        parser.add_argument("--clause-id", default="")
        parser.add_argument("--doc-id", default="")
        parser.add_argument("--top-k", type=int, default=20)
        parser.add_argument("--output", type=Path)

    def handle(self, *args, **options):
        try:
            package = EventLookupPackageBuilder().build(
                EventCutInput(
                    event_cut_id=options["event_cut_id"],
                    document_id=options["doc_id"],
                    clause_id=options["clause_id"],
                    trigger=options["trigger"],
                    event_cut_text=options["event_cut_text"],
                    source="command",
                ),
                top_k=options["top_k"],
            )
            if options["output"]:
                write_event_lookup_package(package, options["output"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"package_id={package['package_id']} candidates={len(package['candidates'])} "
            f"backend={package['backend_name']} tfidf_used=false "
            f"output={options['output'] or 'not_written'}"
        )
