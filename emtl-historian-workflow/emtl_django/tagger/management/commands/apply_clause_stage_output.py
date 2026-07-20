from __future__ import annotations

import copy
import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tagger.models import Document, StageOutput
from tagger.services.clause_application import (
    APPLICATION_AUDIT_KEY,
    ClauseApplicationError,
    build_clause_application_plan,
    generation_provenance,
    json_sha256,
    text_sha256,
)
from tagger.services.clause_persistence import replace_document_clauses


class Command(BaseCommand):
    help = (
        "Apply one validated clause-parser-header-body-v1 StageOutput through the "
        "formal clause persistence service without calling any model."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--stage-output-id", type=int, required=True)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report the exact replacement without writing the database.",
        )

    def handle(self, *args, **options) -> None:
        stage_output_id = int(options["stage_output_id"])
        dry_run = bool(options["dry_run"])
        try:
            if dry_run:
                stage_output = _get_stage_output(stage_output_id)
                scope_before = _scope_snapshot(stage_output)
                plan = build_clause_application_plan(stage_output)
                scope_after = _scope_snapshot(_get_stage_output(stage_output_id))
                report = {
                    **plan.report,
                    "mode": "dry-run",
                    "database_write_performed": False,
                    "integrity": _scope_integrity(scope_before, scope_after),
                }
            else:
                report = self._apply(stage_output_id)
        except StageOutput.DoesNotExist as exc:
            raise CommandError(f"StageOutput not found: {stage_output_id}") from exc
        except ClauseApplicationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    def _apply(self, stage_output_id: int) -> dict[str, Any]:
        with transaction.atomic():
            stage_output = _get_stage_output(stage_output_id, for_update=True)
            document = Document.objects.select_for_update().get(pk=stage_output.document_id)
            stage_output.document = document
            scope_before = _scope_snapshot(stage_output)
            plan = build_clause_application_plan(stage_output)
            if plan.already_applied:
                scope_after = _scope_snapshot(stage_output)
                return {
                    **plan.report,
                    "mode": "write",
                    "database_write_performed": False,
                    "idempotent_noop": True,
                    "integrity": _scope_integrity(scope_before, scope_after),
                }

            applied_at = timezone.now().isoformat()
            clause_replacement_performed = not plan.clauses_already_materialized
            if clause_replacement_performed:
                replace_document_clauses(document, plan.clauses)
            provenance = copy.deepcopy(dict(stage_output.provenance or {}))
            provenance["clauses_applied_to_document"] = True
            provenance[APPLICATION_AUDIT_KEY] = {
                "source": "validated_frozen_stage_output",
                "command": "apply_clause_stage_output",
                "stage_output_id": stage_output.pk,
                "contract_version": plan.report["output_contract_version"],
                "applied_at": applied_at,
                "before_clause_count": plan.report["before"]["clause_count"],
                "before_clauses_sha256": plan.report["before"]["clauses_sha256"],
                "after_clause_count": plan.report["after"]["clause_count"],
                "after_clauses_sha256": plan.report["after"]["clauses_sha256"],
                "raw_output_sha256": plan.report["baselines"]["raw_output_sha256"],
                "working_source_text_sha256": plan.report["baselines"][
                    "working_source_text_sha256"
                ],
                "header_sha256": plan.report["header"]["sha256"],
                "body_sha256": plan.report["body"]["sha256"],
                "provider_called": False,
                "model_called": False,
                "regeneration_performed": False,
            }
            provenance["continued_at"] = applied_at
            provenance["passed_forward_locally"] = True
            stage_output.provenance = provenance
            stage_output.status = StageOutput.Status.ACCEPTED
            stage_output.save(update_fields=["status", "provenance", "updated_at"])

            refreshed = _get_stage_output(stage_output_id)
            completed_plan = build_clause_application_plan(refreshed)
            if not completed_plan.already_applied:
                raise ClauseApplicationError("Post-write application audit was not recognized.")
            scope_after = _scope_snapshot(refreshed)
            integrity = _scope_integrity(scope_before, scope_after)
            if not all(integrity.values()):
                raise ClauseApplicationError(
                    "Post-write scope or preservation check failed; transaction was rolled back."
                )
            return {
                **completed_plan.report,
                "before": plan.report["before"],
                "mode": "write",
                "database_write_performed": True,
                "clause_replacement_performed": clause_replacement_performed,
                "idempotent_noop": False,
                "integrity": integrity,
                "applied_at": applied_at,
            }


def _get_stage_output(stage_output_id: int, *, for_update: bool = False) -> StageOutput:
    queryset = StageOutput.objects.select_related("document")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.get(pk=stage_output_id)


def _scope_snapshot(stage_output: StageOutput) -> dict[str, Any]:
    document = stage_output.document
    target_document = {
        "id": document.pk,
        "doc_id": document.doc_id,
        "archival_reference": document.archival_reference,
        "title": document.title,
        "document_type": document.document_type,
        "normalized_date": document.normalized_date,
        "source_file": document.source_file,
        "metadata": document.metadata,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }
    clauses = list(
        document.clauses.order_by("sequence", "pk").values(
            "clause_id", "sequence", "text"
        )
    )
    other_documents = list(
        Document.objects.exclude(pk=document.pk).order_by("pk").values()
    )
    other_stage_outputs = list(
        StageOutput.objects.exclude(pk=stage_output.pk).order_by("pk").values()
    )
    return {
        "target_document_sha256": json_sha256(target_document),
        "working_source_text_sha256": text_sha256(
            str((document.metadata or {}).get("working_source_text") or "").strip()
        ),
        "target_clauses": clauses,
        "raw_output_sha256": text_sha256(stage_output.raw_output),
        "payload_sha256": json_sha256(stage_output.payload),
        "generation_provenance_sha256": json_sha256(
            generation_provenance(dict(stage_output.provenance or {}))
        ),
        "stage_identity_sha256": json_sha256(
            {
                "id": stage_output.pk,
                "document_id": stage_output.document_id,
                "stage": stage_output.stage,
                "display_title": stage_output.display_title,
                "created_at": stage_output.created_at.isoformat(),
            }
        ),
        "other_documents_sha256": json_sha256(other_documents),
        "other_stage_outputs_sha256": json_sha256(other_stage_outputs),
    }


def _scope_integrity(before: dict[str, Any], after: dict[str, Any]) -> dict[str, bool]:
    return {
        "document_header_metadata_source_unchanged": before["target_document_sha256"]
        == after["target_document_sha256"],
        "working_source_text_unchanged": before["working_source_text_sha256"]
        == after["working_source_text_sha256"],
        "raw_output_unchanged": before["raw_output_sha256"] == after["raw_output_sha256"],
        "payload_provider_generation_evidence_unchanged": before["payload_sha256"]
        == after["payload_sha256"],
        "generation_provenance_unchanged": before["generation_provenance_sha256"]
        == after["generation_provenance_sha256"],
        "stage_identity_unchanged": before["stage_identity_sha256"]
        == after["stage_identity_sha256"],
        "other_documents_unchanged": before["other_documents_sha256"]
        == after["other_documents_sha256"],
        "other_stage_outputs_unchanged": before["other_stage_outputs_sha256"]
        == after["other_stage_outputs_sha256"],
    }
