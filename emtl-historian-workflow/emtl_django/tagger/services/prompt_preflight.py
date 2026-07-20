from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .chatbot_bundle import ChatbotBundleManifest, KnowledgeFileLoader, project_root
from .contracts import (
    ProviderApiPayload,
    StageExecutionRequest,
    normalize_provider_label,
)
from .stage_runner import (
    DEFAULT_LOCAL_MAX_TOKENS,
    ChatbotStageRunner,
    _accepted_upstream_stage_ids,
    _document_header_and_body,
    _prompt_source_completeness,
    _provider_api_inputs,
    _required_upstream_stage_ids,
)
from .stage_validation import validate_required_outputs


PROMPT_BUDGETS = {
    "compact": 32_000,
    "medium": 64_000,
    "large": 128_000,
}

RUNTIME_MODE_COMPACT_DIAGNOSTIC = "compact_diagnostic"
RUNTIME_MODE_SOURCE_COMPLETE_READINESS = "source_complete_readiness"
RUNTIME_MODES = (
    RUNTIME_MODE_COMPACT_DIAGNOSTIC,
    RUNTIME_MODE_SOURCE_COMPLETE_READINESS,
)

DEFAULT_PREFLIGHT_STAGES = (
    "summary_keywords",
    "entity_registry",
    "clause_parser",
    "occurrences_registry",
    "tag_assembler",
)

STAGES_WITH_OUTPUT_VALIDATORS = {
    "entity_registry": {
        "has_output_parser": True,
        "has_output_validator": True,
        "parser": "parse_entity_output",
        "validator": "validate_entity_output",
    },
    "clause_parser": {
        "has_output_parser": True,
        "has_output_validator": True,
        "parser": "parse_clause_output",
        "validator": "validate_clause_coverage",
    }
}


@dataclass(frozen=True)
class PromptPreflightOptions:
    budget_name: str = "compact"
    budget_chars: int | None = None
    provider: str = "gpu_local"
    runtime_mode: str = RUNTIME_MODE_COMPACT_DIAGNOSTIC

    @property
    def resolved_budget_chars(self) -> int:
        if self.budget_chars is not None:
            return max(1, int(self.budget_chars))
        return PROMPT_BUDGETS.get(self.budget_name, PROMPT_BUDGETS["compact"])

    @property
    def resolved_runtime_mode(self) -> str:
        if self.runtime_mode in RUNTIME_MODES:
            return self.runtime_mode
        return RUNTIME_MODE_COMPACT_DIAGNOSTIC


def synthetic_preflight_document(text: str = "") -> SimpleNamespace:
    body = text.strip() or "First preflight paragraph.\n\nSecond preflight paragraph."
    return SimpleNamespace(
        doc_id="preflight-synthetic-document",
        archival_reference="",
        title="Preflight synthetic document",
        document_type="preflight",
        normalized_date="",
        metadata={
            "working_source_text": body,
            "document_title": "Preflight synthetic document",
        },
    )


def build_prompt_package_preflight_report(
    *,
    document: Any,
    stage_outputs: dict[str, Any] | None = None,
    stage_ids: list[str] | tuple[str, ...] | None = None,
    options: PromptPreflightOptions | None = None,
    runner: ChatbotStageRunner | None = None,
) -> dict[str, Any]:
    options = options or PromptPreflightOptions()
    runner = runner or ChatbotStageRunner()
    stage_outputs = stage_outputs or {}
    selected_stage_ids = tuple(stage_ids or DEFAULT_PREFLIGHT_STAGES)
    budget_chars = options.resolved_budget_chars
    return {
        "schema_version": "emtl-prompt-package-preflight-v1",
        "runtime_mode": options.resolved_runtime_mode,
        "budget": {
            "name": options.budget_name,
            "characters": budget_chars,
            "available_budgets": PROMPT_BUDGETS,
        },
        "provider": normalize_provider_label(options.provider),
        "model_called": False,
        "source_faithfulness_policy": {
            "source_files_are_immutable": True,
            "compact_diagnostic_allows_reporting_truncation": True,
            "source_complete_readiness_requires_no_missing_or_truncated_required_files": True,
            "available_retrieval_resources_are_reported_but_do_not_need_normal_prompt_loading": True,
        },
        "document": {
            "document_id": str(getattr(document, "doc_id", "")),
            "document_title": str(getattr(document, "title", "")),
            "document_type": str(getattr(document, "document_type", "")),
        },
        "stages": [
            build_stage_prompt_preflight(
                document=document,
                stage_outputs=stage_outputs,
                stage_id=stage_id,
                options=options,
                runner=runner,
            )
            for stage_id in selected_stage_ids
        ],
    }


def build_stage_prompt_preflight(
    *,
    document: Any,
    stage_outputs: dict[str, Any],
    stage_id: str,
    options: PromptPreflightOptions | None = None,
    runner: ChatbotStageRunner | None = None,
) -> dict[str, Any]:
    options = options or PromptPreflightOptions()
    runner = runner or ChatbotStageRunner()
    budget_chars = options.resolved_budget_chars
    provider_label = normalize_provider_label(options.provider)
    bundle = runner.manifest.get_stage(stage_id)
    document_header, document_body = _document_header_and_body(document)
    required_stage_ids = tuple(_required_upstream_stage_ids(stage_id))
    required_validation = validate_required_outputs(required_stage_ids, stage_outputs)
    system_prompt, user_prompt, package_provenance = runner._build_prompt_package(
        bundle=bundle,
        document_header=document_header,
        source_body=document_body,
        stage_outputs=stage_outputs,
        correction="",
    )
    request = StageExecutionRequest(
        stage_id=stage_id,
        stage_label=bundle.stage_label,
        provider=provider_label,
        requested_provider=provider_label,
        document_id=str(getattr(document, "doc_id", "")),
        document_title=str(getattr(document, "title", "")),
        document_type=str(getattr(document, "document_type", "")),
        required_stage_ids=required_stage_ids,
        accepted_upstream_stage_ids=_accepted_upstream_stage_ids(stage_outputs),
        source_character_count=len(document_body),
        prompt_character_count=len(system_prompt) + len(user_prompt),
        metadata={
            "manifest": str(runner.manifest.path.relative_to(project_root())),
            "expected_output_type": bundle.expected_output_type,
            "prompt_package_built": True,
        },
    )
    provider_payload = ProviderApiPayload(
        request=request,
        inputs=_provider_api_inputs(
            document_header=document_header,
            document_body=document_body,
            stage_outputs={} if stage_id == "entity_registry" else stage_outputs,
        ),
        prompt_package={
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prompt_character_count": len(system_prompt) + len(user_prompt),
            "loaded_files": package_provenance.get("loaded_files", []),
            "source_completeness": _prompt_source_completeness(
                bundle=bundle,
                loaded_files=package_provenance.get("loaded_files", []),
                package_provenance=package_provenance,
            ),
            "indexed_resources": package_provenance.get("indexed_resources", []),
            "entity_candidate_package": package_provenance.get("entity_candidate_package", {}),
            "system_prompt_resolution": package_provenance.get("system_prompt_resolution", {}),
            "event_candidate_packages": package_provenance.get("event_candidate_packages", []),
        },
        options={
            "timeout_seconds": 300,
            "max_output_tokens": DEFAULT_LOCAL_MAX_TOKENS,
        },
    )
    redacted_payload = provider_payload.as_redacted_dict()
    output_capability = STAGES_WITH_OUTPUT_VALIDATORS.get(
        stage_id,
        {
            "has_output_parser": False,
            "has_output_validator": False,
            "parser": "",
            "validator": "",
        },
    )
    total_chars = len(system_prompt) + len(user_prompt)
    loaded_files = list(package_provenance.get("loaded_files", []))
    source_completeness = _source_completeness(
        bundle=bundle,
        loaded_files=loaded_files,
        runtime_mode=options.resolved_runtime_mode,
        package_provenance=package_provenance,
    )
    dependency_ready = bool(required_validation.get("valid", False))
    budget_fits = total_chars <= budget_chars
    return {
        "stage_id": stage_id,
        "stage_label": bundle.stage_label,
        "runtime_mode": options.resolved_runtime_mode,
        "expected_output_type": bundle.expected_output_type,
        "input_requirements": list(bundle.input_requirements),
        "document": {
            "header_character_count": len(document_header),
            "body_character_count": len(document_body),
        },
        "prompt": {
            "system_prompt_character_count": len(system_prompt),
            "user_prompt_character_count": len(user_prompt),
            "total_estimated_prompt_characters": total_chars,
        },
        "loaded_files": loaded_files,
        "loaded_file_count": len(loaded_files),
        "loaded_file_character_count": sum(
            int(item.get("characters_loaded") or 0)
            for item in loaded_files
            if isinstance(item, dict)
        ),
        "loaded_file_truncation": [
            item for item in loaded_files if isinstance(item, dict) and item.get("truncated")
        ],
        "declared_missing_files": list(bundle.missing_files),
        "source_completeness": source_completeness,
        "source_complete": source_completeness["source_complete"],
        "blocking_source_issues": source_completeness["blocking_source_issues"],
        "truncated_required_files": source_completeness["truncated_required_files"],
        "missing_required_files": source_completeness["missing_required_files"],
        "referenced_but_not_loaded_resources": source_completeness["referenced_but_not_loaded_resources"],
        "can_claim_emily_reproduction": (
            options.resolved_runtime_mode == RUNTIME_MODE_SOURCE_COMPLETE_READINESS
            and
            source_completeness["source_complete"]
            and dependency_ready
            and budget_fits
        ),
        "ambiguity_flags": list(bundle.ambiguity_flags),
        "upstream_dependencies": {
            "required_stage_ids": list(required_stage_ids),
            "accepted_upstream_stage_ids": list(request.accepted_upstream_stage_ids),
            "validation": required_validation,
            "available_upstream_outputs": _upstream_output_counts(stage_outputs),
        },
        "budget": {
            "name": options.budget_name,
            "characters": budget_chars,
            "fits": budget_fits,
            "over_by": max(0, total_chars - budget_chars),
        },
        "output_validation": output_capability,
        "provider_payload_shape": {
            "schema_version": provider_payload.as_dict()["schema_version"],
            "payload_schema_version": provider_payload.as_dict()["payload_schema_version"],
            "has_inputs": True,
            "has_prompt_package": True,
            "input_keys": sorted(provider_payload.as_dict()["inputs"].keys()),
            "prompt_package_keys": sorted(provider_payload.as_dict()["prompt_package"].keys()),
            "option_keys": sorted(provider_payload.as_dict()["options"].keys()),
        },
        "provenance_redaction": {
            "redacted": bool(redacted_payload.get("redacted")),
            "document_body_removed": "document_body" not in redacted_payload.get("inputs", {}),
            "document_body_character_count": redacted_payload.get("inputs", {}).get("document_body_character_count", 0),
            "system_prompt_present": redacted_payload.get("prompt_package", {}).get("system_prompt_present", False),
            "user_prompt_present": redacted_payload.get("prompt_package", {}).get("user_prompt_present", False),
            "system_prompt_character_count": redacted_payload.get("prompt_package", {}).get("system_prompt_character_count", 0),
            "user_prompt_character_count": redacted_payload.get("prompt_package", {}).get("user_prompt_character_count", 0),
        },
        "model_called": False,
    }


def _source_completeness(
    *,
    bundle: Any,
    loaded_files: list[Any],
    runtime_mode: str,
    package_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package_provenance = package_provenance or {}
    truncated_required_files = [
        {
            "path": str(item.get("path") or ""),
            "characters_loaded": int(item.get("characters_loaded") or 0),
            "characters_available": int(item.get("characters_available") or 0),
            "truncation_limit": int(item.get("characters_loaded") or 0),
        }
        for item in loaded_files
        if isinstance(item, dict) and item.get("truncated")
    ]
    missing_required_files = [
        {
            "path": str(path),
            "reason": "declared_missing_in_manifest",
        }
        for path in getattr(bundle, "missing_files", ())
    ]
    referenced_but_not_loaded_resources = [
        {
            "path": _relative_path(path),
            "role": "retrieval_resource",
            "exists": Path(path).exists(),
            "reason": "manifest_retrieval_resource_not_loaded_as_normal_prompt_knowledge",
        }
        for path in getattr(bundle, "retrieval_resource_paths", ())
    ]
    blocking_source_issues: list[dict[str, Any]] = []
    for item in truncated_required_files:
        blocking_source_issues.append(
            {
                "code": "truncated_required_file",
                "path": item["path"],
                "message": "A manifest-selected source file was loaded only partially.",
            }
        )
    for item in missing_required_files:
        blocking_source_issues.append(
            {
                "code": "missing_required_file",
                "path": item["path"],
                "message": "A required source file is declared missing in the manifest.",
            }
        )
    for item in referenced_but_not_loaded_resources:
        if not item["exists"]:
            blocking_source_issues.append(
                {
                    "code": "missing_retrieval_resource",
                    "path": item["path"],
                    "message": "A manifest retrieval resource is required but not available.",
                }
            )
    entity_indexed_resources: list[dict[str, Any]] = []
    entity_candidate_package: dict[str, Any] = {}
    if getattr(bundle, "stage_id", "") == "entity_registry":
        entity_indexed_resources = list(package_provenance.get("indexed_resources") or [])
        entity_candidate_package = dict(package_provenance.get("entity_candidate_package") or {})
        expected_indexed_count = len(getattr(bundle, "controlled_list_paths", ()))
        indexes_complete = (
            len(entity_indexed_resources) == expected_indexed_count
            and all(
                bool(item.get("fully_indexed")) and bool(item.get("source_hash"))
                for item in entity_indexed_resources
            )
        )
        if not indexes_complete:
            blocking_source_issues.append(
                {
                    "code": "entity_registry_index_incomplete",
                    "message": "All three built-in Entity registry resources must be fully indexed.",
                }
            )
        if not bool(
            entity_candidate_package.get("source_complete")
            and entity_candidate_package.get("provenance_complete")
        ):
            blocking_source_issues.append(
                {
                    "code": "entity_candidate_package_incomplete",
                    "message": "The bounded Entity candidate package lacks complete source provenance.",
                }
            )
        if not bool(
            entity_candidate_package.get("selection_policy_complete")
            and entity_candidate_package.get("mandatory_candidates_retained")
            and entity_candidate_package.get("candidate_package_complete")
        ):
            blocking_source_issues.append(
                {
                    "code": "entity_candidate_selection_incomplete",
                    "message": "The deterministic Entity candidate policy was not executed completely.",
                }
            )
    return {
        "runtime_mode": runtime_mode,
        "source_complete": not blocking_source_issues,
        "can_claim_source_complete_reproduction_readiness": (
            runtime_mode == RUNTIME_MODE_SOURCE_COMPLETE_READINESS
            and not blocking_source_issues
        ),
        "blocking_source_issues": blocking_source_issues,
        "truncated_required_files": truncated_required_files,
        "missing_required_files": missing_required_files,
        "referenced_but_not_loaded_resources": referenced_but_not_loaded_resources,
        "resource_access_mode": (
            "full_index_plus_bounded_candidates"
            if getattr(bundle, "stage_id", "") == "entity_registry"
            else "prompt_files"
        ),
        "indexed_resources": entity_indexed_resources,
        "entity_candidate_package": entity_candidate_package,
    }


def _relative_path(path: Any) -> str:
    candidate = Path(path)
    try:
        return str(candidate.relative_to(project_root()))
    except ValueError:
        return str(candidate)


def _upstream_output_counts(stage_outputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    counts = {}
    for stage_id, stage_output in stage_outputs.items():
        payload = getattr(stage_output, "payload", {}) or {}
        provenance = getattr(stage_output, "provenance", {}) or {}
        counts[stage_id] = {
            "status": str(getattr(stage_output, "status", "") or ""),
            "raw_output_character_count": len(str(getattr(stage_output, "raw_output", "") or "")),
            "payload_key_count": len(payload) if isinstance(payload, dict) else 0,
            "provenance_key_count": len(provenance) if isinstance(provenance, dict) else 0,
        }
    return counts
