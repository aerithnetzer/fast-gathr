from django.core.management.base import BaseCommand, CommandError

from tagger.services.entity_persistence import promote_entity_attempt_to_review_candidate


class Command(BaseCommand):
    help = "Promote an eligible stored real Entity attempt to checking without rerunning the model."

    def add_arguments(self, parser):
        parser.add_argument("--attempt-id", type=int, required=True)
        parser.add_argument("--confirm-review-candidate", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_review_candidate"]:
            raise CommandError("Refusing promotion without --confirm-review-candidate.")
        try:
            outcome = promote_entity_attempt_to_review_candidate(attempt_id=options["attempt_id"])
        except (ValueError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"attempt_id={outcome.attempt_id} stage_output_id={outcome.stage_output_id} "
                "state=checking approved_for_downstream=false"
            )
        )
