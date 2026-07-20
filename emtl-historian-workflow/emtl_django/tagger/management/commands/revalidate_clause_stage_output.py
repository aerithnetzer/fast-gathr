from __future__ import annotations

import hashlib
import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tagger.models import StageOutput
from tagger.services.clause_revalidation import (
    ClauseRevalidationError,
    build_clause_revalidation_plan,
)


class Command(BaseCommand):
    help = (
        "Revalidate one completed Clause Parser StageOutput from its stored raw_output; "
        "never applies Document clauses."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--stage-output-id", type=int, required=True)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the deterministic update plan without writing the database.",
        )

    def handle(self, *args, **options) -> None:
        stage_output_id = int(options["stage_output_id"])
        dry_run = bool(options["dry_run"])
        try:
            if dry_run:
                stage_output = _get_stage_output(stage_output_id)
                integrity_before = _integrity_snapshot(stage_output)
                plan = build_clause_revalidation_plan(stage_output)
                integrity_after = _integrity_snapshot(_get_stage_output(stage_output_id))
                report = {
                    **plan.report,
                    "mode": "dry-run",
                    "database_write_performed": False,
                    "idempotent_noop": not plan.report["semantic_change_required"],
                    "integrity": _integrity_report(integrity_before, integrity_after),
                }
            else:
                with transaction.atomic():
                    stage_output = _get_stage_output(stage_output_id, for_update=True)
                    integrity_before = _integrity_snapshot(stage_output)
                    plan = build_clause_revalidation_plan(stage_output)
                    changed = (
                        stage_output.payload != plan.payload
                        or stage_output.provenance != plan.provenance
                    )
                    if changed:
                        stage_output.payload = plan.payload
                        stage_output.provenance = plan.provenance
                        stage_output.save(update_fields=["payload", "provenance"])
                    refreshed = _get_stage_output(stage_output_id)
                    integrity_after = _integrity_snapshot(refreshed)
                    integrity = _integrity_report(integrity_before, integrity_after)
                    if not all(integrity.values()):
                        raise ClauseRevalidationError(
                            "Post-write integrity check failed; transaction was rolled back."
                        )
                    report = {
                        **plan.report,
                        "mode": "write",
                        "database_write_performed": changed,
                        "idempotent_noop": not changed,
                        "integrity": integrity,
                    }
        except StageOutput.DoesNotExist as exc:
            raise CommandError(f"StageOutput not found: {stage_output_id}") from exc
        except ClauseRevalidationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _get_stage_output(stage_output_id: int, *, for_update: bool = False) -> StageOutput:
    queryset = StageOutput.objects.select_related("document")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.get(pk=stage_output_id)


def _integrity_snapshot(stage_output: StageOutput) -> dict[str, Any]:
    document = stage_output.document
    clauses = list(
        document.clauses.order_by("sequence", "pk").values(
            "id", "clause_id", "text", "sequence", "start_char", "end_char"
        )
    )
    return {
        "raw_output_sha256": _sha256(stage_output.raw_output),
        "lifecycle_status": stage_output.status,
        "document_metadata_sha256": _json_sha256(document.metadata),
        "working_source_text_sha256": _sha256(
            str((document.metadata or {}).get("working_source_text") or "")
        ),
        "document_clause_count": len(clauses),
        "document_clauses_sha256": _json_sha256(clauses),
    }


def _integrity_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, bool]:
    return {
        "raw_output_unchanged": before["raw_output_sha256"] == after["raw_output_sha256"],
        "lifecycle_status_unchanged": before["lifecycle_status"] == after["lifecycle_status"],
        "document_metadata_unchanged": before["document_metadata_sha256"]
        == after["document_metadata_sha256"],
        "working_source_text_unchanged": before["working_source_text_sha256"]
        == after["working_source_text_sha256"],
        "document_clause_count_unchanged": before["document_clause_count"]
        == after["document_clause_count"],
        "document_clauses_unchanged": before["document_clauses_sha256"]
        == after["document_clauses_sha256"],
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(serialized)
