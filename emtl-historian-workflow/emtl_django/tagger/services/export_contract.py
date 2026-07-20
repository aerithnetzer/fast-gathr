from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tagger.models import Document, NewIdProposal, ReviewNote, StageExecutionAttempt, StageOutput

from .entity_review_handoff import build_entity_downstream_package, entity_downstream_is_eligible
from .review_decisions import build_review_decisions_for_export


SCHEMA_VERSION = "emtl-workflow-export-v1"
CONTRACT_VERSION = "emtl-handoff-contract-v1"


def build_workflow_export(document: Document) -> dict[str, Any]:
    stages = list(document.stage_outputs.all().order_by("created_at", "id"))
    stage_map = {row.stage: row for row in stages}
    attempts = list(
        StageExecutionAttempt.objects.filter(stage_output__document=document)
        .select_related("stage_output")
        .order_by("created_at", "id")
    )
    notes = list(
        ReviewNote.objects.filter(document=document)
        .select_related("stage_output", "clause")
        .order_by("created_at", "id")
    )
    proposals = list(
        NewIdProposal.objects.filter(document=document)
        .select_related("source_clause")
        .order_by("created_at", "id")
    )
    decisions = build_review_decisions_for_export(
        document_id=document.doc_id,
        stage_outputs=stage_map,
        stage_notes=notes,
        proposals=proposals,
    )
    accepted_stages = {
        stage_id: _stage_record(output)
        for stage_id, output in stage_map.items()
        if output.status == StageOutput.Status.ACCEPTED
    }
    accepted_data: dict[str, Any] = {
        "stage_outputs": accepted_stages,
        "final_review_decisions": [row for row in decisions if row.get("is_final")],
        "entity_registry": {},
        "event_assignments": [],
        "occurrences": {},
        "assembled_tagset": {},
    }
    entity = stage_map.get(StageOutput.Stage.ENTITY_REGISTRY)
    if entity and entity_downstream_is_eligible(entity):
        accepted_data["entity_registry"] = build_entity_downstream_package(entity)
    eventcut = stage_map.get(StageOutput.Stage.EVENTCUT_EXTRACTION)
    if eventcut and eventcut.status == StageOutput.Status.ACCEPTED:
        accepted_data["event_assignments"] = list(
            (eventcut.payload or {}).get("accepted_assignments") or []
        )
    occurrence = stage_map.get(StageOutput.Stage.OCCURRENCES_REGISTRY)
    if occurrence and occurrence.status == StageOutput.Status.ACCEPTED:
        accepted_data["occurrences"] = _stage_record(occurrence)
    assembler = stage_map.get(StageOutput.Stage.TAG_ASSEMBLER)
    if assembler and assembler.status == StageOutput.Status.ACCEPTED:
        accepted_data["assembled_tagset"] = _stage_record(assembler)

    package = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "export_id": "",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "document": {
            "database_id": document.pk,
            "document_id": document.doc_id,
            "archival_reference": document.archival_reference,
            "title": document.title,
            "document_type": document.document_type,
            "normalized_date": document.normalized_date,
            "source_file": document.source_file,
            "metadata": document.metadata or {},
        },
        "clauses": [
            {
                "clause_id": row.clause_id,
                "sequence": row.sequence,
                "text": row.text,
                "start_char": row.start_char,
                "end_char": row.end_char,
            }
            for row in document.clauses.all().order_by("sequence", "id")
        ],
        "accepted_data": accepted_data,
        "review": {
            "decisions": decisions,
            "proposals": [_proposal_record(row) for row in proposals],
            "notes": [_note_record(row) for row in notes],
        },
        "orchestration": (document.metadata or {}).get("workflow_orchestrator", {}),
        "audit": {
            "stage_outputs": [_stage_record(row) for row in stages],
            "execution_attempts": [_attempt_record(row) for row in attempts],
        },
        "artifact_handoff": {
            "source_object": _artifact_placeholder("source"),
            "export_object": _artifact_placeholder("workflow_export"),
            "additional_objects": [],
        },
        "integrity": {},
    }
    content_hash = _semantic_hash(package)
    package["export_id"] = f"export-{content_hash[:24]}"
    package["integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_hash(package),
        "semantic_content_sha256": content_hash,
        "accepted_stage_ids": sorted(accepted_stages),
        "nonaccepted_stage_ids_audit_only": sorted(
            stage_id for stage_id, output in stage_map.items()
            if output.status != StageOutput.Status.ACCEPTED
        ),
    }
    validation = validate_workflow_export(package)
    if not validation["valid"]:
        raise ValueError("Invalid workflow export: " + "; ".join(validation["issues"]))
    return package


def validate_workflow_export(package: dict[str, Any]) -> dict[str, Any]:
    issues = []
    if package.get("schema_version") != SCHEMA_VERSION:
        issues.append("schema_version")
    if package.get("contract_version") != CONTRACT_VERSION:
        issues.append("contract_version")
    if not str((package.get("document") or {}).get("document_id") or ""):
        issues.append("document.document_id")
    audit_stages = (package.get("audit") or {}).get("stage_outputs") or []
    accepted = ((package.get("accepted_data") or {}).get("stage_outputs") or {})
    audit_by_stage = {row.get("stage_id"): row for row in audit_stages if isinstance(row, dict)}
    for stage_id, row in accepted.items():
        if row.get("status") != StageOutput.Status.ACCEPTED:
            issues.append(f"accepted_data.stage_outputs.{stage_id}.status")
        if (audit_by_stage.get(stage_id) or {}).get("stage_output_id") != row.get("stage_output_id"):
            issues.append(f"accepted_data.stage_outputs.{stage_id}.audit_link")
    occurrence = (package.get("accepted_data") or {}).get("occurrences") or {}
    if occurrence and occurrence.get("status") != StageOutput.Status.ACCEPTED:
        issues.append("accepted_data.occurrences.status")
    assembler = (package.get("accepted_data") or {}).get("assembled_tagset") or {}
    if assembler and assembler.get("status") != StageOutput.Status.ACCEPTED:
        issues.append("accepted_data.assembled_tagset.status")
    integrity = package.get("integrity") or {}
    if integrity.get("canonical_json_sha256") != _canonical_hash(package):
        issues.append("integrity.canonical_json_sha256")
    if integrity.get("semantic_content_sha256") != _semantic_hash(package):
        issues.append("integrity.semantic_content_sha256")
    return {"valid": not issues, "issues": issues}


def write_workflow_export(*, document: Document, output_path: Path) -> dict[str, Any]:
    package = build_workflow_export(document)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    return package


def _stage_record(row: StageOutput) -> dict[str, Any]:
    return {
        "stage_output_id": row.pk,
        "stage_id": row.stage,
        "display_title": row.display_title,
        "status": row.status,
        "raw_output": row.raw_output,
        "payload": row.payload or {},
        "provenance": row.provenance or {},
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _attempt_record(row: StageExecutionAttempt) -> dict[str, Any]:
    return {
        "attempt_id": row.pk,
        "stage_output_id": row.stage_output_id,
        "request_id": row.request_id,
        "stage_id": row.stage,
        "execution_status": row.execution_status,
        "disposition": row.disposition,
        "provider": row.provider,
        "model": row.model,
        "raw_output": row.raw_output,
        "payload": row.payload or {},
        "provenance": row.provenance or {},
        "validation": row.validation or {},
        "error": row.error,
        "applied_to_stage_output": row.applied_to_stage_output,
        "created_at": row.created_at.isoformat(),
    }


def _proposal_record(row: NewIdProposal) -> dict[str, Any]:
    return {
        "proposal_id": row.pk,
        "proposed_id": row.proposed_id,
        "record_type": row.record_type,
        "headword": row.headword,
        "evidence_form": row.evidence_form,
        "source_clause_id": row.source_clause.clause_id if row.source_clause else "",
        "status": row.status,
        "reviewer_note": row.reviewer_note,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _note_record(row: ReviewNote) -> dict[str, Any]:
    return {
        "note_id": row.pk,
        "stage_output_id": row.stage_output_id,
        "clause_id": row.clause.clause_id if row.clause else "",
        "note": row.note,
        "requested_action": row.requested_action,
        "created_by": row.created_by_label,
        "created_at": row.created_at.isoformat(),
    }


def _artifact_placeholder(role: str) -> dict[str, Any]:
    return {"role": role, "status": "not_uploaded", "uri": "", "bucket": "", "key": "", "version_id": "", "sha256": ""}


def _canonical_hash(package: dict[str, Any]) -> str:
    value = json.loads(json.dumps(package))
    value["integrity"] = {}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _semantic_hash(package: dict[str, Any]) -> str:
    value = json.loads(json.dumps(package))
    value["integrity"] = {}
    value["export_id"] = ""
    value["exported_at"] = ""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
