from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

from .chatbot_bundle import ChatbotBundleManifest, KnowledgeFileLoader, StageBundle, project_root
from .contracts import (
    ExecutionStatus,
    ProviderLabel,
    STAGE_EXECUTION_RESULT_SCHEMA,
    STAGE_PROVIDER_API_PAYLOAD_SCHEMA,
    ProviderApiPayload,
    StageExecutionRequest,
    StageExecutionResult,
    build_provider_provenance,
    normalize_provider_label,
    now_iso,
)
from .event_lookup import build_default_event_lookup_service
from .entity_knowledge import EntityKnowledgeRetriever
from .entity_output import entity_model_output_contract_text, parse_and_validate_entity_output
from .entity_review_handoff import build_entity_downstream_package, entity_downstream_is_eligible
from .providers.gpu_local import GpuLocalProviderClient
from .providers.factory import stage_generation_client
from .stage_validation import (
    parse_clause_output_structure,
    validate_clause_coverage,
    validate_required_outputs,
)


DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/generate"
DEFAULT_LOCAL_MODEL = "qwen2.5:3b"
DEFAULT_LOCAL_MAX_TOKENS = 4096


@dataclass(frozen=True)
class ProviderResponse:
    status: str
    text: str
    provider: str
    model: str
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    real_chatbot_execution: bool | None = None


class StageProvider(Protocol):
    provider_name: str

    def generate(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        """Generate one stage response."""


class LocalOllamaProvider:
    provider_name = ProviderLabel.LOCAL_CPU.value

    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        timeout: int = 300,
    ) -> None:
        self.endpoint = endpoint or os.getenv("EMTL_OLLAMA_ENDPOINT") or DEFAULT_OLLAMA_ENDPOINT
        self.model = model or os.getenv("EMTL_LOCAL_LLM_MODEL") or DEFAULT_LOCAL_MODEL
        self.timeout = int(os.getenv("EMTL_OLLAMA_TIMEOUT_SECONDS") or timeout)
        self.context_size = _optional_positive_int(os.getenv("EMTL_OLLAMA_CONTEXT"))
        self.max_tokens = int(
            os.getenv("EMTL_OLLAMA_MAX_TOKENS") or DEFAULT_LOCAL_MAX_TOKENS
        )
        self.think = _optional_bool(os.getenv("EMTL_OLLAMA_THINK"))

    def generate(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        options = {
            "temperature": 0.1,
            "num_predict": self.max_tokens,
        }
        if self.context_size:
            options["num_ctx"] = self.context_size
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": f"{system_prompt}\n\n{user_prompt}",
            "stream": False,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                timeout=(5, self.timeout),
            )
            response.raise_for_status()
            data = response.json()
            text = str(data.get("response") or "").strip()
            if not text:
                raise RuntimeError("The local model returned an empty response.")
            return ProviderResponse(
                status="completed",
                text=text,
                provider=self.provider_name,
                model=self.model,
                real_chatbot_execution=True,
            )
        except Exception as exc:
            return ProviderResponse(
                status="unavailable",
                text="",
                provider=self.provider_name,
                model=self.model,
                error=str(exc),
                errors=[
                    {
                        "code": "local_cpu_unavailable",
                        "message": str(exc),
                    }
                ],
                real_chatbot_execution=False,
            )


class BackendStubProvider:
    provider_name = ProviderLabel.BACKEND_STUB.value

    def generate(self, system_prompt: str, user_prompt: str) -> ProviderResponse:
        return ProviderResponse(
            status=ExecutionStatus.UNAVAILABLE.value,
            text="",
            provider=self.provider_name,
            model="not-configured",
            error="Backend stub provider only; no external API call is configured.",
            errors=[
                {
                    "code": "provider_unavailable",
                    "message": "Backend stub provider only; no external API call is configured.",
                }
            ],
            real_chatbot_execution=False,
        )


ApiStubProvider = BackendStubProvider
StageRunResult = StageExecutionResult


def _optional_positive_int(value: str | None) -> int | None:
    if not str(value or "").strip():
        return None
    parsed = int(str(value).strip())
    return parsed if parsed > 0 else None


def _optional_bool(value: str | None) -> bool | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Unsupported boolean value: {value}")


class ChatbotStageRunner:
    def __init__(
        self,
        manifest: ChatbotBundleManifest | None = None,
        knowledge_loader: KnowledgeFileLoader | None = None,
        entity_knowledge_retriever: EntityKnowledgeRetriever | None = None,
    ) -> None:
        self.manifest = manifest or ChatbotBundleManifest()
        self.knowledge_loader = knowledge_loader or KnowledgeFileLoader()
        self.entity_knowledge_retriever = entity_knowledge_retriever

    def run(
        self,
        *,
        stage_id: str,
        document: Any,
        stage_outputs: dict[str, Any],
        correction: str = "",
        provider: str = "local",
        fixture_payload: dict[str, Any] | None = None,
        fixture_raw_output: str = "",
    ) -> StageRunResult:
        bundle = self.manifest.get_stage(stage_id)
        provider_label = normalize_provider_label(provider)
        header, source_body = _document_header_and_body(document)
        required_stage_ids = tuple(_required_upstream_stage_ids(stage_id))
        requirement_check = validate_required_outputs(
            required_stage_ids,
            stage_outputs,
        )
        started_at = now_iso()
        request = StageExecutionRequest(
            stage_id=stage_id,
            stage_label=bundle.stage_label,
            provider=provider_label,
            requested_provider=provider,
            document_id=str(getattr(document, "doc_id", "")),
            document_title=str(getattr(document, "title", "")),
            document_type=str(getattr(document, "document_type", "")),
            required_stage_ids=required_stage_ids,
            accepted_upstream_stage_ids=_accepted_upstream_stage_ids(stage_outputs),
            correction_requested=bool(correction.strip()),
            source_character_count=len(source_body),
            metadata={
                "manifest": str(self.manifest.path.relative_to(project_root())),
                "expected_output_type": bundle.expected_output_type,
            },
        )

        if provider_label == ProviderLabel.FIXTURE.value:
            status = ExecutionStatus.COMPLETED.value
            provenance = build_provider_provenance(
                request=request,
                provider=provider_label,
                model="",
                status=status,
                started_at=started_at,
                real_chatbot_execution=False,
                extra={
                    "source": "fixture_fallback",
                    "manifest": str(self.manifest.path.relative_to(project_root())),
                    "fixture_execution": True,
                },
            )
            return StageRunResult(
                status=status,
                raw_output=fixture_raw_output,
                payload=fixture_payload or {},
                provenance=provenance,
                provider=provider_label,
                model="",
                request=request.as_dict(),
            )

        if stage_id == "entity_registry":
            status = ExecutionStatus.BLOCKED.value
            errors = [
                {
                    "code": "entity_generation_requires_controlled_command",
                    "message": (
                        "Real Entity Registry generation is available only through the "
                        "explicit entity_registry_gpu_generate management command."
                    ),
                }
            ]
            provenance = build_provider_provenance(
                request=request,
                provider=provider_label,
                model="",
                status=status,
                started_at=started_at,
                real_chatbot_execution=False,
                errors=errors,
            )
            return StageRunResult(
                status=status,
                raw_output="",
                payload={},
                provenance=provenance,
                error=errors[0]["message"],
                provider=provider_label,
                errors=errors,
                request=request.as_dict(),
            )

        if not requirement_check["valid"]:
            status = ExecutionStatus.BLOCKED.value
            errors = [
                {
                    "code": "missing_required_upstream_outputs",
                    "message": "Required upstream stage outputs are missing.",
                    "details": requirement_check,
                }
            ]
            provenance = build_provider_provenance(
                request=request,
                provider=provider_label,
                model="",
                status=status,
                started_at=started_at,
                real_chatbot_execution=False,
                validation={"required_outputs": requirement_check},
                errors=errors,
            )
            return StageRunResult(
                status=status,
                raw_output="",
                payload={"required_output_validation": requirement_check},
                provenance=provenance,
                error="Required upstream stage outputs are missing.",
                provider=provider_label,
                validation={"required_outputs": requirement_check},
                errors=errors,
                request=request.as_dict(),
            )

        try:
            system_prompt, user_prompt, package_provenance = self._build_prompt_package(
                bundle=bundle,
                document_header=header,
                source_body=source_body,
                stage_outputs=stage_outputs,
                correction=correction,
            )
            request = StageExecutionRequest(
                **{
                    **request.as_dict(),
                    "prompt_character_count": len(system_prompt) + len(user_prompt),
                    "required_stage_ids": tuple(request.required_stage_ids),
                    "accepted_upstream_stage_ids": tuple(request.accepted_upstream_stage_ids),
                    "metadata": {
                        **request.metadata,
                        "prompt_package_built": True,
                        "provider_api_payload_schema": STAGE_PROVIDER_API_PAYLOAD_SCHEMA,
                    },
                }
            )
            provider_api_payload = ProviderApiPayload(
                request=request,
                inputs=_provider_api_inputs(
                    document_header=header,
                    document_body=source_body,
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
                    "prompt_component_summary": package_provenance.get("prompt_component_summary", {}),
                    "system_prompt_resolution": package_provenance.get("system_prompt_resolution", {}),
                    "event_candidate_packages": package_provenance.get("event_candidate_packages", []),
                },
                options={
                    "timeout_seconds": 3600,
                    "max_output_tokens": DEFAULT_LOCAL_MAX_TOKENS,
                },
            )
        except Exception as exc:
            status = ExecutionStatus.BLOCKED.value
            errors = [
                {
                    "code": "prompt_package_build_failed",
                    "message": f"Prompt package could not be built: {exc}",
                }
            ]
            provenance = build_provider_provenance(
                request=request,
                provider=provider_label,
                model="",
                status=status,
                started_at=started_at,
                real_chatbot_execution=False,
                errors=errors,
            )
            return StageRunResult(
                status=status,
                raw_output="",
                payload={},
                provenance=provenance,
                error=f"Prompt package could not be built: {exc}",
                provider=provider_label,
                errors=errors,
                request=request.as_dict(),
            )

        response: ProviderResponse
        if provider_label == ProviderLabel.LOCAL_CPU.value:
            provider_impl: StageProvider
            provider_impl = LocalOllamaProvider()
            response = provider_impl.generate(system_prompt, user_prompt)
        elif provider_label == ProviderLabel.GPU_LOCAL.value:
            gpu_response = stage_generation_client(provider_label).generate(
                provider_api_payload.as_dict()
            )
            response = ProviderResponse(
                status=gpu_response.status,
                text=gpu_response.raw_output,
                provider=gpu_response.provider,
                model=gpu_response.model,
                error=gpu_response.error,
                payload=gpu_response.payload,
                validation=gpu_response.validation,
                warnings=gpu_response.warnings,
                errors=gpu_response.errors,
                metadata=gpu_response.metadata,
                real_chatbot_execution=gpu_response.real_chatbot_execution,
            )
        elif provider_label in {
            ProviderLabel.AWS_BEDROCK.value,
            ProviderLabel.EXTERNAL_API.value,
        }:
            if provider_label == ProviderLabel.AWS_BEDROCK.value:
                from .providers.aws_bedrock import BedrockProviderClient

                provider_impl = BedrockProviderClient()
                response = provider_impl.generate_text(system_prompt, user_prompt)
            else:
                provider_impl = BackendStubProvider()
                response = provider_impl.generate(system_prompt, user_prompt)
        else:
            provider_impl = BackendStubProvider()
            response = provider_impl.generate(system_prompt, user_prompt)
        response_provider = provider_label if provider_label != ProviderLabel.LOCAL_CPU.value else response.provider
        validation = {
            "required_outputs": requirement_check,
            "declared_missing_files": list(bundle.missing_files),
            "ambiguity_flags": list(bundle.ambiguity_flags),
            **response.validation,
        }
        warnings = list(bundle.ambiguity_flags)
        if bundle.missing_files:
            warnings.extend(f"Declared missing file: {filename}" for filename in bundle.missing_files)
        warnings.extend(response.warnings)
        response_errors = response.errors or (
            [
                {
                    "code": "provider_unavailable",
                    "message": response.error,
                }
            ]
            if response.status != ExecutionStatus.COMPLETED.value
            else []
        )
        real_chatbot_execution = (
            response.real_chatbot_execution
            if response.real_chatbot_execution is not None
            else response.status == ExecutionStatus.COMPLETED.value
        )
        provenance = build_provider_provenance(
            request=request,
            provider=response_provider,
            model=response.model,
            status=response.status,
            started_at=started_at,
            real_chatbot_execution=real_chatbot_execution,
            validation=validation,
            warnings=warnings,
            errors=response_errors,
            extra={
                "manifest": str(self.manifest.path.relative_to(project_root())),
                "bundle": package_provenance,
                "provider_api_payload_schema": STAGE_PROVIDER_API_PAYLOAD_SCHEMA,
                "provider_api_payload": provider_api_payload.as_redacted_dict(),
                "provider_response_metadata": response.metadata,
                "declared_missing_files": list(bundle.missing_files),
                "ambiguity_flags": list(bundle.ambiguity_flags),
            },
        )
        if response.status != ExecutionStatus.COMPLETED.value:
            return StageRunResult(
                status=response.status,
                raw_output="",
                payload={
                    "runner_status": response.status,
                    "runner_error": response.error,
                    "fixture_fallback_preserved": True,
                    "request": request.as_dict(),
                    "provider_api_payload": provider_api_payload.as_redacted_dict(),
                    "validation": validation,
                },
                provenance=provenance,
                error=response.error,
                provider=provider_label,
                model=response.model,
                validation=validation,
                warnings=warnings,
                errors=response_errors,
                request=request.as_dict(),
            )

        payload = self._parse_stage_output(
            stage_id=stage_id,
            raw_output=response.text,
            expected_header=header,
            source_body=source_body,
        )
        if stage_id == "entity_registry":
            entity_validation = dict(payload.get("entity_validation") or {})
            validation["entity_output"] = entity_validation
            provenance["validation"] = validation
            provenance["entity_registry"] = {
                "registry_version": entity_validation.get("registry_version", ""),
                "resource_hashes": entity_validation.get("resource_hashes", {}),
                "validation_valid": bool(entity_validation.get("valid")),
            }
            if not bool(entity_validation.get("valid")):
                status = ExecutionStatus.VALIDATION_FAILED.value
                errors = [
                    {
                        "code": "entity_output_validation_failed",
                        "message": "Entity Registry output failed structured validation.",
                        "details": entity_validation,
                    }
                ]
                provenance["execution_status"] = status
                provenance["errors"] = errors
                return StageRunResult(
                    status=status,
                    raw_output=response.text,
                    payload=payload,
                    provenance=provenance,
                    error="Entity Registry output failed structured validation.",
                    provider=provider_label,
                    model=response.model,
                    validation=validation,
                    warnings=warnings,
                    errors=errors,
                    request=request.as_dict(),
                )
        if response.payload:
            payload["provider_payload"] = response.payload
        if stage_id == "occurrences_registry":
            payload["event_lookup_candidates"] = package_provenance.get("event_candidate_packages", [])
        payload["runner_contract"] = {
            "request_schema": request.schema_version,
            "result_schema": STAGE_EXECUTION_RESULT_SCHEMA,
            "provider_api_payload_schema": STAGE_PROVIDER_API_PAYLOAD_SCHEMA,
            "provider": provider_label,
            "status": response.status,
        }
        return StageRunResult(
            status=ExecutionStatus.COMPLETED.value,
            raw_output=response.text,
            payload=payload,
            provenance=provenance,
            provider=provider_label,
            model=response.model,
            validation=validation,
            warnings=warnings,
            request=request.as_dict(),
        )

    def _build_prompt_package(
        self,
        *,
        bundle: StageBundle,
        document_header: str,
        source_body: str,
        stage_outputs: dict[str, Any],
        correction: str,
        include_tokenization_diagnostics: bool = False,
    ) -> tuple[str, str, dict[str, Any]]:
        if not bundle.system_prompt_path.exists():
            raise FileNotFoundError(bundle.system_prompt_path)
        system_prompt, system_meta = self.knowledge_loader.read(bundle.system_prompt_path)
        expected_system_hash = str(bundle.system_prompt_resolution.get("sha256") or "")
        if expected_system_hash and system_meta.get("sha256") != expected_system_hash:
            raise RuntimeError(
                "Resolved Entity system prompt hash does not match the manifest authority record."
            )
        knowledge_sections = []
        knowledge_component_texts: list[tuple[str, str]] = []
        loaded_files = [system_meta]
        prompt_knowledge_paths = bundle.knowledge_paths
        if bundle.stage_id == "entity_registry":
            prompt_knowledge_paths = (
                bundle.instruction_paths
                + bundle.example_paths
                + bundle.boilerplate_paths
            )
        for path in prompt_knowledge_paths:
            if not path.exists():
                raise FileNotFoundError(path)
            text, metadata = self.knowledge_loader.read(path)
            loaded_files.append(metadata)
            if path in bundle.instruction_paths:
                component_name = "instructions"
            elif path in bundle.boilerplate_paths:
                component_name = "legal_boilerplate"
            else:
                component_name = f"knowledge:{metadata['path']}"
            knowledge_component_texts.append((component_name, text))
            knowledge_sections.append(
                f"===== KNOWLEDGE FILE: {metadata['path']} =====\n{text}"
            )

        stage_input = _stage_input_text(
            stage_id=bundle.stage_id,
            document_header=document_header,
            source_body=source_body,
            stage_outputs=stage_outputs,
        )
        event_candidates = []
        indexed_resources = []
        entity_candidate_package: dict[str, Any] = {}
        entity_candidate_model_text = ""
        entity_candidate_v1_text = ""
        entity_candidate_section_text = ""
        entity_candidate_v1_section_text = ""
        if bundle.stage_id == "entity_registry":
            retriever = self.entity_knowledge_retriever or EntityKnowledgeRetriever()
            entity_knowledge = retriever.build(
                document_header=document_header,
                document_body=source_body,
            )
            indexed_resources = list(entity_knowledge.indexed_resources)
            entity_candidate_package = entity_knowledge.provenance_dict()
            entity_candidate_model_text = entity_knowledge.prompt_text()
            entity_candidate_v1_text = entity_knowledge.legacy_v1_prompt_text()
            entity_candidate_section_text = (
                "===== ENTITY REGISTRY BOUNDED KNOWLEDGE CANDIDATES =====\n"
                "The three controlled registry resources were fully indexed by the platform. "
                "This entity-bounded-knowledge-v2 package retains every exact, normalized, "
                "and Watch List-required candidate; fuzzy rows are deterministic ranked "
                "supplements. Rows use the declared compact columns and every included string "
                "and field value is complete, not truncated. Full source provenance is carried "
                "out of band. Final IDs must match the structured registry validator; fuzzy or "
                "vector similarity never selects an ID.\n"
                + entity_candidate_model_text
            )
            entity_candidate_v1_section_text = (
                "===== ENTITY REGISTRY BOUNDED KNOWLEDGE CANDIDATES =====\n"
                "The three controlled registry resources were fully indexed by the platform. "
                "The following source-provenanced records are candidates only. Final IDs must "
                "match the structured registry validator; fuzzy or vector similarity never "
                "selects an ID.\n"
                + entity_candidate_v1_text
            )
            knowledge_sections.append(entity_candidate_section_text)
            knowledge_sections.append(entity_model_output_contract_text())
        if bundle.stage_id == "occurrences_registry":
            event_candidates = build_event_candidate_packages(stage_outputs, source_body)
            if event_candidates:
                stage_input += (
                    "\n\n===== DEVELOPMENT EVENT HEADWORD CANDIDATES =====\n"
                    "These candidates are retrieval hints only. Select by meaning; do not force a fit.\n"
                    + json.dumps(event_candidates, ensure_ascii=False, indent=2)
                )
        if correction.strip():
            stage_input += (
                "\n\n===== HUMAN CORRECTION REQUEST =====\n"
                f"{correction.strip()}\n"
                "Regenerate the stage output while preserving every unaffected decision."
            )
        user_prompt = (
            "\n\n".join(knowledge_sections)
            + "\n\n===== STAGE INPUT =====\n"
            + stage_input
            + "\n\nReturn only the stage output required by the system prompt."
        )
        component_summary: dict[str, Any] = {}
        tokenization_diagnostics: dict[str, Any] = {}
        if bundle.stage_id == "entity_registry":
            component_summary, tokenization_diagnostics = _entity_prompt_component_diagnostics(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                knowledge_component_texts=knowledge_component_texts,
                candidate_model_text=entity_candidate_model_text,
                candidate_v1_text=entity_candidate_v1_text,
                candidate_section_text=entity_candidate_section_text,
                candidate_v1_section_text=entity_candidate_v1_section_text,
                document_header=document_header,
                source_body=source_body,
                candidate_provenance=entity_candidate_package,
                include_tokenization_diagnostics=include_tokenization_diagnostics,
            )
        return system_prompt, user_prompt, {
            "stage_label": bundle.stage_label,
            "expected_output_type": bundle.expected_output_type,
            "loaded_files": loaded_files,
            "prompt_characters": len(system_prompt) + len(user_prompt),
            "event_candidate_packages": event_candidates,
            "indexed_resources": indexed_resources,
            "entity_candidate_package": entity_candidate_package,
            "prompt_component_summary": component_summary,
            "tokenization_diagnostics": tokenization_diagnostics,
            "system_prompt_resolution": dict(bundle.system_prompt_resolution),
        }

    def _parse_stage_output(
        self,
        *,
        stage_id: str,
        raw_output: str,
        expected_header: str,
        source_body: str,
    ) -> dict[str, Any]:
        if stage_id == "clause_parser":
            parsed = parse_clause_output_structure(raw_output)
            validation = validate_clause_coverage(
                source_body,
                parsed.clauses,
                expected_header=expected_header,
                generated_header=parsed.generated_header,
            )
            return {
                "output_contract_version": "clause-parser-header-body-v1",
                "generated_header": parsed.generated_header,
                "header_was_bracket_wrapped": parsed.header_was_bracket_wrapped,
                "clauses": parsed.clauses,
                "coverage_validation": validation.as_dict(),
                "notice": validation.message,
                "generated_output": raw_output,
            }
        if stage_id == "entity_registry":
            parsed, validation = parse_and_validate_entity_output(
                raw_output,
                expected_header=expected_header,
                source_body=source_body,
            )
            return {
                "output_contract_version": "entity-registry-structured-v1",
                "entity_output": parsed.as_dict(),
                "entity_validation": validation.as_dict(),
                "generated_output": raw_output,
            }
        payload = {
            "generated_output": raw_output,
        }
        return payload


def _prompt_component_span(
    *,
    name: str,
    role: str,
    prompt: str,
    component_text: str,
    search_start: int,
) -> dict[str, Any]:
    start = prompt.find(component_text, max(0, int(search_start)))
    if start < 0:
        raise RuntimeError(f"Prompt component could not be located without truncation: {name}")
    end = start + len(component_text)
    return {
        "name": name,
        "role": role,
        "start": start,
        "end": end,
        "characters": len(component_text),
        "sha256": hashlib.sha256(component_text.encode("utf-8")).hexdigest(),
    }


def _entity_prompt_component_diagnostics(
    *,
    system_prompt: str,
    user_prompt: str,
    knowledge_component_texts: list[tuple[str, str]],
    candidate_model_text: str,
    candidate_v1_text: str,
    candidate_section_text: str,
    candidate_v1_section_text: str,
    document_header: str,
    source_body: str,
    candidate_provenance: dict[str, Any],
    include_tokenization_diagnostics: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    component_spans: list[dict[str, Any]] = [
        _prompt_component_span(
            name="system_prompt",
            role="system",
            prompt=system_prompt,
            component_text=system_prompt,
            search_start=0,
        )
    ]
    user_search_start = 0
    for component_name, component_text in knowledge_component_texts:
        span = _prompt_component_span(
            name=component_name,
            role="user",
            prompt=user_prompt,
            component_text=component_text,
            search_start=user_search_start,
        )
        component_spans.append(span)
        user_search_start = int(span["end"])
    candidate_span = _prompt_component_span(
        name="candidate_model_visible",
        role="user",
        prompt=user_prompt,
        component_text=candidate_model_text,
        search_start=user_search_start,
    )
    component_spans.append(candidate_span)
    header_span = _prompt_component_span(
        name="document_header",
        role="user",
        prompt=user_prompt,
        component_text=document_header,
        search_start=int(candidate_span["end"]),
    )
    component_spans.append(header_span)
    body_span = _prompt_component_span(
        name="document_body",
        role="user",
        prompt=user_prompt,
        component_text=source_body,
        search_start=int(header_span["end"]),
    )
    component_spans.append(body_span)
    component_character_total = sum(int(item["characters"]) for item in component_spans)
    component_summary = {
        "contract_version": "prompt-component-spans-v2",
        "components": component_spans,
        "model_visible_prompt_characters": len(system_prompt) + len(user_prompt),
        "model_visible_wrapper_characters": (
            len(system_prompt) + len(user_prompt) - component_character_total
        ),
        "out_of_band_provenance_characters": int(
            candidate_provenance.get("out_of_band_provenance_character_count") or 0
        ),
    }
    if not include_tokenization_diagnostics:
        return component_summary, {}
    v1_user_prompt = user_prompt.replace(
        candidate_section_text,
        candidate_v1_section_text,
        1,
    )
    if v1_user_prompt == user_prompt:
        raise RuntimeError("Entity v1/v2 tokenizer comparison prompt could not be constructed.")
    v1_spans = [dict(component_spans[0])]
    v1_search_start = 0
    for component_name, component_text in knowledge_component_texts:
        span = _prompt_component_span(
            name=component_name,
            role="user",
            prompt=v1_user_prompt,
            component_text=component_text,
            search_start=v1_search_start,
        )
        v1_spans.append(span)
        v1_search_start = int(span["end"])
    v1_candidate_span = _prompt_component_span(
        name="candidate_model_visible",
        role="user",
        prompt=v1_user_prompt,
        component_text=candidate_v1_text,
        search_start=v1_search_start,
    )
    v1_spans.append(v1_candidate_span)
    v1_header_span = _prompt_component_span(
        name="document_header",
        role="user",
        prompt=v1_user_prompt,
        component_text=document_header,
        search_start=int(v1_candidate_span["end"]),
    )
    v1_spans.append(v1_header_span)
    v1_spans.append(
        _prompt_component_span(
            name="document_body",
            role="user",
            prompt=v1_user_prompt,
            component_text=source_body,
            search_start=int(v1_header_span["end"]),
        )
    )
    return component_summary, {
        "contract_version": "entity-tokenizer-component-comparison-v2",
        "current": {
            "label": "entity-bounded-knowledge-v2",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "component_spans": component_spans,
        },
        "baseline": {
            "label": "entity-bounded-knowledge-v1",
            "system_prompt": system_prompt,
            "user_prompt": v1_user_prompt,
            "component_spans": v1_spans,
        },
        "out_of_band_provenance": candidate_provenance,
    }


def _working_source_text(document: Any) -> str:
    metadata = dict(getattr(document, "metadata", {}) or {})
    text = str(metadata.get("working_source_text") or "").strip()
    if text:
        return text
    clauses = getattr(document, "clauses", None)
    if clauses is None:
        return ""
    return "\n\n".join(str(clause.text) for clause in clauses.all())


def _document_header(document: Any) -> str:
    metadata = dict(getattr(document, "metadata", {}) or {})
    fields = [
        ("DocID", getattr(document, "doc_id", "")),
        ("Archival Reference", getattr(document, "archival_reference", "")),
        ("Record Title", metadata.get("title") or getattr(document, "title", "")),
        ("Document Title", metadata.get("document_title") or getattr(document, "title", "")),
        ("Document Type", getattr(document, "document_type", "")),
        ("Originating Body", metadata.get("originating_body", "")),
        ("Plaintiff", metadata.get("plaintiff", "")),
        ("Defendant", metadata.get("defendant", "")),
        ("Normalized Date", getattr(document, "normalized_date", "")),
    ]
    lines = [f"{label}: {value}" for label, value in fields if str(value or "").strip()]
    return "\n".join(lines + ["<END>"])


HEADER_END_MARKER = re.compile(r"(?im)^[ \t]*<END>[ \t]*(?:\r?\n|$)")


def _split_structured_header_body(value: str) -> tuple[str, str] | None:
    text = str(value or "").strip()
    match = HEADER_END_MARKER.search(text)
    if match is None:
        return None
    header = text[: match.end()].strip()
    body = text[match.end() :].strip()
    return header, body


def _document_header_and_body(document: Any) -> tuple[str, str]:
    working_text = _working_source_text(document)
    structured = _split_structured_header_body(working_text)
    if structured is not None:
        return structured
    return _document_header(document), working_text


def _required_upstream_stage_ids(stage_id: str) -> tuple[str, ...]:
    return {
        "occurrences_registry": ("clause_parser", "entity_registry"),
        "tag_assembler": ("clause_parser", "entity_registry", "occurrences_registry"),
    }.get(stage_id, ())


def _accepted_upstream_stage_ids(stage_outputs: dict[str, Any]) -> tuple[str, ...]:
    accepted = []
    for stage_id, stage_output in stage_outputs.items():
        status = str(getattr(stage_output, "status", "") or "")
        entity_approved = entity_downstream_is_eligible(stage_output)
        if status == "accepted" and (stage_id != "entity_registry" or entity_approved):
            accepted.append(str(stage_id))
    return tuple(sorted(accepted))


def _stage_input_text(
    *,
    stage_id: str,
    document_header: str,
    source_body: str,
    stage_outputs: dict[str, Any],
) -> str:
    if stage_id in {"summary_keywords", "entity_registry", "clause_parser"}:
        return f"{document_header}\n\n{source_body}"
    clause_output = str(getattr(stage_outputs.get("clause_parser"), "raw_output", "") or "")
    entity_stage_output = stage_outputs.get("entity_registry")
    entity_output = ""
    if entity_stage_output is not None:
        entity_output = json.dumps(
            build_entity_downstream_package(entity_stage_output),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if stage_id == "occurrences_registry":
        return (
            f"{document_header}\n\n"
            f"===== PARSED CLAUSES =====\n{clause_output}\n\n"
            f"===== ENTITY REGISTRY =====\n{entity_output}"
        )
    occurrence_output = str(getattr(stage_outputs.get("occurrences_registry"), "raw_output", "") or "")
    if stage_id == "tag_assembler":
        return (
            f"{document_header}\n\n"
            f"===== PARSED CLAUSES =====\n{clause_output}\n\n"
            f"===== ENTITY REGISTRY =====\n{entity_output}\n\n"
            f"===== OCCURRENCES REGISTRY =====\n{occurrence_output}"
        )
    return f"{document_header}\n\n{source_body}"


def _provider_api_inputs(
    *,
    document_header: str,
    document_body: str,
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "document_header": document_header,
        "document_body": document_body,
        "upstream_outputs": {
            stage_id: {
                "status": str(getattr(stage_output, "status", "") or ""),
                "raw_output": str(getattr(stage_output, "raw_output", "") or ""),
                "payload": getattr(stage_output, "payload", {}) or {},
                "provenance": getattr(stage_output, "provenance", {}) or {},
            }
            for stage_id, stage_output in stage_outputs.items()
        },
    }


def _prompt_source_completeness(
    *,
    bundle: StageBundle,
    loaded_files: list[dict[str, Any]],
    package_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    truncated = [
        {
            "path": str(item.get("path") or ""),
            "characters_loaded": int(item.get("characters_loaded") or 0),
            "characters_available": int(item.get("characters_available") or 0),
        }
        for item in loaded_files
        if bool(item.get("truncated"))
        or int(item.get("characters_available") or 0)
        > int(item.get("characters_loaded") or 0)
    ]
    missing = [str(path) for path in bundle.missing_files]
    package_provenance = package_provenance or {}
    if bundle.stage_id == "entity_registry":
        required_file_count = 1 + len(bundle.instruction_paths) + len(bundle.example_paths) + len(bundle.boilerplate_paths)
        indexed_resources = list(package_provenance.get("indexed_resources") or [])
        candidate_package = dict(package_provenance.get("entity_candidate_package") or {})
        indexed_resources_complete = (
            len(indexed_resources) == len(bundle.controlled_list_paths)
            and all(bool(item.get("fully_indexed")) and item.get("source_hash") for item in indexed_resources)
        )
        candidate_package_complete = bool(
            candidate_package.get("source_complete")
            and candidate_package.get("provenance_complete")
            and candidate_package.get("selection_policy_complete")
            and candidate_package.get("mandatory_candidates_retained")
            and candidate_package.get("candidate_package_complete")
        )
    else:
        required_file_count = 1 + len(bundle.knowledge_paths)
        indexed_resources = []
        indexed_resources_complete = True
        candidate_package_complete = True
    source_complete = (
        not truncated
        and not missing
        and len(loaded_files) == required_file_count
        and indexed_resources_complete
        and candidate_package_complete
    )
    return {
        "source_complete": source_complete,
        "required_file_count": required_file_count,
        "loaded_file_count": len(loaded_files),
        "missing_required_files": missing,
        "truncated_required_files": truncated,
        "resource_access_mode": (
            "full_index_plus_bounded_candidates"
            if bundle.stage_id == "entity_registry"
            else "prompt_files"
        ),
        "indexed_resource_count": len(indexed_resources),
        "indexed_resources_complete": indexed_resources_complete,
        "candidate_package_complete": candidate_package_complete,
    }


def build_event_candidate_packages(
    stage_outputs: dict[str, Any],
    source_body: str,
) -> list[dict[str, Any]]:
    clause_payload = getattr(stage_outputs.get("clause_parser"), "payload", {}) or {}
    clauses = list(clause_payload.get("clauses") or [])
    if not clauses:
        clauses = [{"clause_id": "source", "text": source_body}]
    service = build_default_event_lookup_service()
    packages = []
    for clause in clauses[:20]:
        text = str(clause.get("text") or "").strip()
        if not text:
            continue
        packages.append(
            {
                "clause_id": str(clause.get("clause_id") or ""),
                "query_scope": "whole_clause_development_fallback",
                "candidates": service.lookup(text, top_k=5),
            }
        )
    return packages
