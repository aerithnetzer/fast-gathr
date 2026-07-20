from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tagger.models import Clause, Document, StageOutput
from tagger.stage_config import STAGE_PROFILES
from tagger.services.clause_persistence import replace_document_clauses
from tagger.services.contracts import ExecutionStatus, ProviderLabel
from tagger.services.stage_runner import ChatbotStageRunner, StageRunResult


DEFAULT_TEXT = "First smoke paragraph.\n\nSecond smoke paragraph."


class Command(BaseCommand):
    help = (
        "Create or reuse a compact uploaded-document smoke record, run Clause Parser "
        "through the gpu_local provider, and persist the StageOutput."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--doc-id",
            default="gpu-local-uploaded-workflow-smoke",
            help="Document id to create or reuse.",
        )
        parser.add_argument(
            "--title",
            default="GPU-local uploaded workflow smoke document",
            help="Document title for a newly created smoke document.",
        )
        parser.add_argument(
            "--text",
            default=DEFAULT_TEXT,
            help="Uploaded-document-like working text. Blank lines split clauses.",
        )
        parser.add_argument(
            "--stage",
            default="clause_parser",
            choices=["clause_parser"],
            help="Stage to run. The smoke command is intentionally limited to Clause Parser.",
        )
        parser.add_argument(
            "--existing-only",
            action="store_true",
            help="Require --doc-id to identify an existing document; never create a smoke record.",
        )

    def handle(self, *args, **options) -> None:
        doc_id = str(options["doc_id"])
        if options["existing_only"]:
            document = Document.objects.filter(doc_id=doc_id).first()
            if document is None:
                raise CommandError(f"Existing document not found: {doc_id}")
        else:
            document = _get_or_create_uploaded_smoke_document(
                doc_id=doc_id,
                title=str(options["title"]),
                text=str(options["text"]),
            )
        result = run_gpu_local_stage_for_document(
            document=document,
            stage_id=str(options["stage"]),
        )
        stage_output = StageOutput.objects.get(document=document, stage=str(options["stage"]))
        summary = {
            "document_id": document.doc_id,
            "stage_id": stage_output.stage,
            "stage_output_id": stage_output.pk,
            "stage_lifecycle_status": stage_output.status,
            "execution_status": result.status,
            "provider": stage_output.provenance.get("provider", ""),
            "model": stage_output.provenance.get("model", ""),
            "real_chatbot_execution": stage_output.provenance.get("real_chatbot_execution", False),
            "coverage_validation": result.payload.get("coverage_validation", {}),
            "errors": stage_output.provenance.get("errors", []),
        }
        self.stdout.write(json.dumps(summary, indent=2))
        if result.status == ExecutionStatus.BLOCKED.value:
            raise CommandError(result.error or "Stage run was blocked.")


def run_gpu_local_stage_for_document(
    *,
    document: Document,
    stage_id: str = "clause_parser",
) -> StageRunResult:
    if stage_id != "clause_parser":
        raise ValueError("The gpu_local uploaded workflow smoke path only runs Clause Parser.")
    _ensure_stage_outputs(document)
    stage_output = StageOutput.objects.get(document=document, stage=stage_id)
    stage_outputs = {
        output.stage: output
        for output in StageOutput.objects.filter(document=document)
    }
    stage_outputs[stage_id] = stage_output
    result = ChatbotStageRunner().run(
        stage_id=stage_id,
        document=document,
        stage_outputs=stage_outputs,
        provider=ProviderLabel.GPU_LOCAL.value,
    )
    _persist_stage_run_result(stage_output=stage_output, result=result)
    return result


def _get_or_create_uploaded_smoke_document(*, doc_id: str, title: str, text: str) -> Document:
    cleaned_text = _normalize_working_text(text) or DEFAULT_TEXT
    document = Document.objects.filter(doc_id=doc_id).first()
    if document:
        return document
    segments = _split_segments(cleaned_text)
    with transaction.atomic():
        document = Document.objects.create(
            doc_id=doc_id,
            title=title,
            document_type="uploaded document",
            source_file="gpu-local-smoke.txt",
            metadata={
                "doc_id": doc_id,
                "title": title,
                "document_title": title,
                "document_type": "uploaded document",
                "workflow_source": "uploaded_document",
                "upload_extraction_status": "extracted",
                "upload_text_format": "txt",
                "working_source_text": cleaned_text,
                "working_source_text_source": "gpu_local_uploaded_workflow_smoke",
                "working_source_paragraph_count": len(segments),
                "placeholder_outputs": True,
                "real_chatbot_execution": False,
            },
        )
        for index, segment in enumerate(segments, start=1):
            Clause.objects.create(
                document=document,
                clause_id=str(index).zfill(4),
                sequence=index,
                text=segment,
            )
        _ensure_stage_outputs(document)
    return document


def _ensure_stage_outputs(document: Document) -> None:
    for stage_id, profile in STAGE_PROFILES.items():
        StageOutput.objects.get_or_create(
            document=document,
            stage=stage_id,
            defaults={
                "status": StageOutput.Status.BLOCKED
                if profile.future
                else StageOutput.Status.LOADED,
                "display_title": profile.label,
                "payload": {},
                "raw_output": "",
                "provenance": {
                    "source": "uploaded_document_smoke_placeholder",
                    "real_chatbot_execution": False,
                    "postgresql_commit": False,
                },
            },
        )


def _persist_stage_run_result(*, stage_output: StageOutput, result: StageRunResult) -> None:
    if result.status == ExecutionStatus.COMPLETED.value:
        stage_output.payload = {
            **result.payload,
            "runner": {
                "status": result.status,
                "provider": result.provenance.get("provider", ""),
                "model": result.provenance.get("model", ""),
                "message": "Stage generated by the GPU-local provider.",
            },
        }
        stage_output.raw_output = result.raw_output
        stage_output.provenance = result.provenance
        stage_output.status = StageOutput.Status.CHECKING
        if _valid_clause_parser_result(stage_output, result):
            replace_document_clauses(stage_output.document, list(result.payload.get("clauses") or []))
            stage_output.provenance["clauses_applied_to_document"] = True
    else:
        payload = dict(stage_output.payload or {})
        payload["runner"] = {
            "status": result.status,
            "provider": result.provenance.get("provider", ""),
            "model": result.provenance.get("model", ""),
            "message": result.error or "The stage runner did not complete.",
            "fixture_fallback_preserved": True,
        }
        stage_output.payload = payload
        provenance = dict(stage_output.provenance or {})
        provenance.update(result.provenance)
        provenance["last_runner_attempt"] = {
            "status": result.status,
            "error": result.error,
            "fixture_fallback_preserved": True,
        }
        stage_output.provenance = provenance
    stage_output.save(update_fields=["status", "payload", "raw_output", "provenance", "updated_at"])


def _valid_clause_parser_result(stage_output: StageOutput, result: StageRunResult) -> bool:
    return (
        stage_output.stage == "clause_parser"
        and result.status == ExecutionStatus.COMPLETED.value
        and bool((result.payload.get("coverage_validation") or {}).get("valid"))
    )


def _normalize_working_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _split_segments(text: str) -> list[str]:
    normalized = _normalize_working_text(text)
    if not normalized:
        return []
    return [segment.strip() for segment in normalized.split("\n\n") if segment.strip()]
