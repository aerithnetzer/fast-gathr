from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.db import IntegrityError

from tagger.models import Document, StageExecutionAttempt, StageOutput

from .chatbot_bundle import project_root
from .contracts import (
    ExecutionStatus,
    ProviderApiPayload,
    ProviderLabel,
    StageExecutionResult,
    now_iso,
)
from .entity_gpu_preflight import (
    ENTITY_CONTEXT_LIMIT,
    ENTITY_INPUT_SAFETY_MARGIN_TOKENS,
    ENTITY_MAX_OUTPUT_TOKENS,
    ENTITY_TARGET_PROMPT_TOKENS,
    EntityGpuPreflightRunner,
)
from .entity_output import parse_and_validate_entity_output
from .entity_persistence import (
    DuplicateEntityRequestError,
    persist_entity_registry_attempt,
    reserve_entity_registry_attempt,
)
from .entity_registry import ENTITY_REGISTRY_RESOURCES
from .gpu_readiness import (
    ENTITY_GPU_READINESS_PROFILE,
    ENTITY_GPU_READINESS_RUNTIME_MARGIN_MIB,
    validate_latest_readiness_evidence,
)
from .providers.gpu_local import GpuLocalProviderClient
from .providers.factory import local_controlled_generation_client
from .stage_runner import _document_header_and_body


ENTITY_REAL_GENERATION_CONTRACT = "entity-registry-controlled-generation-v1"
ENTITY_ALLOWED_MODEL = "Qwen2.5-32B-Instruct"
ENTITY_ALLOWED_PROVIDER = ProviderLabel.GPU_LOCAL.value


class EntityControlledGenerationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EntityControlledGenerationRunner:
    """Single-request Entity generation path guarded by tokenizer and source gates."""

    def __init__(
        self,
        *,
        preflight_runner: EntityGpuPreflightRunner | None = None,
        client: GpuLocalProviderClient | None = None,
        expected_provider_version: str | None = None,
    ) -> None:
        self.client = client or local_controlled_generation_client()
        self.preflight_runner = preflight_runner or EntityGpuPreflightRunner(
            client=self.client
        )
        self.expected_provider_version = (
            str(expected_provider_version).strip()
            if expected_provider_version is not None
            else _local_provider_runtime_version()
        )

    def run(
        self,
        *,
        document: Document,
        stage_output: StageOutput,
        confirm_real_generation: bool,
        request_id: str | None = None,
        require_readiness_evidence: bool = True,
    ) -> dict[str, Any]:
        if not confirm_real_generation:
            raise EntityControlledGenerationError(
                "real_generation_confirmation_required",
                "Entity generation requires --confirm-real-generation.",
            )
        if stage_output.document_id != document.pk or stage_output.stage != StageOutput.Stage.ENTITY_REGISTRY:
            raise EntityControlledGenerationError(
                "entity_stage_output_mismatch",
                "The selected StageOutput is not this document's Entity Registry stage.",
            )
        if stage_output.status == StageOutput.Status.ACCEPTED:
            raise EntityControlledGenerationError(
                "accepted_entity_output_protected",
                "The existing accepted Entity Registry StageOutput is immutable.",
            )
        clause_output = StageOutput.objects.filter(
            document=document,
            stage=StageOutput.Stage.CLAUSE_PARSER,
            status=StageOutput.Status.ACCEPTED,
        ).first()
        if clause_output is None:
            raise EntityControlledGenerationError(
                "accepted_clause_parser_required",
                "An accepted Clause Parser StageOutput is required before Entity generation.",
            )

        cleaned_request_id = str(request_id or "").strip() or (
            f"entity-registry-real-{uuid4().hex}"
        )
        plan = self.preflight_runner.build_plan(document=document, stage_outputs={})
        request = replace(
            plan.provider_payload.request,
            request_id=cleaned_request_id,
            metadata={
                **plan.provider_payload.request.metadata,
                "operation": "entity_registry_controlled_real_generation",
                "generation_enabled_by": "entity_registry_gpu_generate",
                "real_generation_confirmed": True,
            },
        )
        preflight_payload = ProviderApiPayload(
            request=request,
            inputs=plan.provider_payload.inputs,
            prompt_package=plan.provider_payload.prompt_package,
            options=dict(plan.provider_payload.options),
        )
        local_gate = _validate_local_prompt_package(preflight_payload)
        prompt_fingerprint = _prompt_fingerprint(preflight_payload.prompt_package)
        reservation_provenance = {
            "contract_version": ENTITY_REAL_GENERATION_CONTRACT,
            "request": request.as_dict(),
            "provider": ENTITY_ALLOWED_PROVIDER,
            "model": ENTITY_ALLOWED_MODEL,
            "real_chatbot_execution": False,
            "model_call_attempted": False,
            "model_call_completed": False,
            "reservation_created_at": now_iso(),
            "prompt_fingerprint": prompt_fingerprint,
            "local_source_gate": local_gate,
            "accepted_clause_parser": {
                "stage_output_id": clause_output.pk,
                "clause_count": document.clauses.count(),
            },
            "bundle": _bundle_from_prompt_package(preflight_payload.prompt_package),
        }
        try:
            reserved_attempt = reserve_entity_registry_attempt(
                stage_output=stage_output,
                request_id=cleaned_request_id,
                provenance=reservation_provenance,
            )
        except (DuplicateEntityRequestError, IntegrityError) as exc:
            raise EntityControlledGenerationError(
                "duplicate_entity_request",
                f"This Entity request ID has already been submitted: {cleaned_request_id}",
            ) from exc

        health = self.client.health()
        health_errors = _validate_provider_health(
            health.payload if health.ok else {},
            expected_provider_version=self.expected_provider_version,
        )
        if not health.ok:
            health_errors.insert(0, health.error or "Provider health request failed.")
        if health_errors:
            return self._persist_failure(
                stage_output=stage_output,
                reserved_attempt=reserved_attempt,
                request=request.as_dict(),
                prompt_package=preflight_payload.prompt_package,
                code="provider_health_gate_failed",
                errors=health_errors,
                execution_status=ExecutionStatus.UNAVAILABLE.value,
                health=health.payload,
            )

        tokenization = self.client.tokenize_only(preflight_payload.as_dict())
        preflight_errors = _validate_remote_tokenization(
            tokenization=tokenization,
            local_gate=local_gate,
            prompt_fingerprint=prompt_fingerprint,
        )
        if preflight_errors:
            return self._persist_failure(
                stage_output=stage_output,
                reserved_attempt=reserved_attempt,
                request=request.as_dict(),
                prompt_package=preflight_payload.prompt_package,
                code="tokenizer_preflight_gate_failed",
                errors=preflight_errors,
                execution_status=ExecutionStatus.VALIDATION_FAILED.value,
                health=health.payload,
                tokenization=tokenization,
            )

        prompt_tokens = int(
            ((tokenization.payload or {}).get("token_counts") or {}).get(
                "prompt_tokens"
            )
            or 0
        )
        if require_readiness_evidence:
            readiness_health = self.client.health()
            readiness_errors = _validate_provider_health(
                readiness_health.payload if readiness_health.ok else {},
                expected_provider_version=self.expected_provider_version,
            )
            readiness_errors.extend(
                validate_latest_readiness_evidence(
                    readiness_health.payload if readiness_health.ok else {},
                    expected_provider_version=self.expected_provider_version,
                    prompt_tokens=prompt_tokens,
                )
            )
            if not readiness_health.ok:
                readiness_errors.insert(
                    0,
                    readiness_health.error or "Provider readiness health request failed.",
                )
            if readiness_errors:
                return self._persist_failure(
                    stage_output=stage_output,
                    reserved_attempt=reserved_attempt,
                    request=request.as_dict(),
                    prompt_package=preflight_payload.prompt_package,
                    code="provider_readiness_gate_failed",
                    errors=readiness_errors,
                    execution_status=ExecutionStatus.VALIDATION_FAILED.value,
                    health=readiness_health.payload,
                    tokenization=tokenization,
                )
            health = readiness_health

        generate_prompt_package = dict(preflight_payload.prompt_package)
        generate_prompt_package.pop("tokenization_diagnostics", None)
        generate_payload = ProviderApiPayload(
            request=request,
            inputs=preflight_payload.inputs,
            prompt_package=generate_prompt_package,
            options={
                **preflight_payload.options,
                "tokenization_only": False,
                "generation_enabled": True,
                "operation": "entity_registry_controlled_real_generation",
                "max_output_tokens": ENTITY_MAX_OUTPUT_TOKENS,
            },
        )
        if _prompt_fingerprint(generate_payload.prompt_package) != prompt_fingerprint:
            return self._persist_failure(
                stage_output=stage_output,
                reserved_attempt=reserved_attempt,
                request=request.as_dict(),
                prompt_package=preflight_payload.prompt_package,
                code="prompt_changed_after_preflight",
                errors=["The model-visible prompt changed after tokenizer preflight."],
                execution_status=ExecutionStatus.VALIDATION_FAILED.value,
                health=health.payload,
                tokenization=tokenization,
            )

        # Exactly one generate call. GpuLocalProviderClient has no retry loop.
        response = self.client.generate(generate_payload.as_dict())
        result = _build_execution_result(
            response=response,
            request=request.as_dict(),
            document=document,
            prompt_package=preflight_payload.prompt_package,
            prompt_fingerprint=prompt_fingerprint,
            health=health.payload,
            tokenization=tokenization,
        )
        persistence = persist_entity_registry_attempt(
            stage_output=stage_output,
            result=result,
            reserved_attempt=reserved_attempt,
        )
        return _build_summary(
            result=result,
            persistence=persistence.as_dict(),
            request_id=cleaned_request_id,
            prompt_package=preflight_payload.prompt_package,
            tokenization=tokenization,
            document=document,
            stage_output=stage_output,
        )

    def _persist_failure(
        self,
        *,
        stage_output: StageOutput,
        reserved_attempt: StageExecutionAttempt,
        request: dict[str, Any],
        prompt_package: dict[str, Any],
        code: str,
        errors: list[str],
        execution_status: str,
        health: dict[str, Any],
        tokenization: Any | None = None,
    ) -> dict[str, Any]:
        error_items = [{"code": code, "message": item} for item in errors]
        result = StageExecutionResult(
            status=execution_status,
            raw_output="",
            payload={"entity_validation": _invalid_validation(error_items)},
            provenance={
                "contract_version": ENTITY_REAL_GENERATION_CONTRACT,
                "request": request,
                "provider": ENTITY_ALLOWED_PROVIDER,
                "model": ENTITY_ALLOWED_MODEL,
                "real_chatbot_execution": False,
                "model_call_attempted": False,
                "model_call_completed": False,
                "provider_health": _redacted_health(health),
                "tokenizer_preflight": _tokenization_audit(tokenization),
                "bundle": _bundle_from_prompt_package(prompt_package),
                "errors": error_items,
            },
            error="; ".join(errors),
            provider=ENTITY_ALLOWED_PROVIDER,
            model=ENTITY_ALLOWED_MODEL,
            validation={"entity_output": _invalid_validation(error_items)},
            errors=error_items,
            request=request,
        )
        persistence = persist_entity_registry_attempt(
            stage_output=stage_output,
            result=result,
            reserved_attempt=reserved_attempt,
        )
        return _build_summary(
            result=result,
            persistence=persistence.as_dict(),
            request_id=str(request.get("request_id") or ""),
            prompt_package=prompt_package,
            tokenization=tokenization,
            document=stage_output.document,
            stage_output=stage_output,
        )


def _local_provider_runtime_version() -> str:
    script = project_root() / "tools" / "gpu_provider_server.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--print-version"],
        cwd=project_root(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise EntityControlledGenerationError(
            "provider_version_unavailable",
            "The local provider runtime version could not be determined safely.",
        )
    return version


def _validate_local_prompt_package(payload: ProviderApiPayload) -> dict[str, Any]:
    prompt_package = payload.prompt_package
    source = dict(prompt_package.get("source_completeness") or {})
    candidate = dict(prompt_package.get("entity_candidate_package") or {})
    loaded_files = list(prompt_package.get("loaded_files") or [])
    indexed = list(prompt_package.get("indexed_resources") or [])
    errors = []
    if payload.request.stage_id != "entity_registry":
        errors.append("stage_id is not entity_registry")
    if payload.request.provider != ENTITY_ALLOWED_PROVIDER:
        errors.append("provider is not gpu_local")
    if int(payload.options.get("max_output_tokens") or 0) != ENTITY_MAX_OUTPUT_TOKENS:
        errors.append("max_output_tokens is not 4096")
    if int(payload.options.get("max_input_tokens") or 0) != ENTITY_TARGET_PROMPT_TOKENS:
        errors.append("max_input_tokens is not 27648")
    if not bool(source.get("source_complete")):
        errors.append("source_complete is false")
    if not bool(source.get("candidate_package_complete")):
        errors.append("candidate_package_complete is false")
    if not bool(
        candidate.get("source_complete")
        and candidate.get("provenance_complete")
        and candidate.get("selection_policy_complete")
        and candidate.get("mandatory_candidates_retained")
        and candidate.get("candidate_package_complete")
    ):
        errors.append("Entity candidate provenance or mandatory selection is incomplete")
    if bool(candidate.get("vector_layer_enabled")):
        errors.append("Entity vector layer must remain disabled")
    if len(loaded_files) != 3 or any(
        bool(item.get("truncated"))
        or not str(item.get("sha256") or "")
        or int(item.get("characters_loaded") or 0)
        != int(item.get("characters_available") or 0)
        for item in loaded_files
    ):
        errors.append("Emily prompt resources are missing or truncated")
    expected_registry_hashes = {item.name: item.sha256 for item in ENTITY_REGISTRY_RESOURCES}
    if candidate.get("resource_hashes") != expected_registry_hashes:
        errors.append("Registry resource hashes differ from the authoritative values")
    if len(indexed) != 3 or any(not item.get("fully_indexed") for item in indexed):
        errors.append("All three registry resources are not fully indexed")
    if errors:
        raise EntityControlledGenerationError(
            "local_prompt_gate_failed",
            "; ".join(errors),
        )
    component_summary = dict(prompt_package.get("prompt_component_summary") or {})
    component_hashes = {
        str(item.get("name") or ""): str(item.get("sha256") or "")
        for item in component_summary.get("components") or []
        if isinstance(item, dict)
    }
    required_components = {
        "system_prompt",
        "instructions",
        "legal_boilerplate",
        "candidate_model_visible",
        "document_header",
        "document_body",
    }
    if set(component_hashes) != required_components or not all(component_hashes.values()):
        raise EntityControlledGenerationError(
            "prompt_component_hashes_incomplete",
            "The final Entity prompt component hashes are incomplete.",
        )
    return {
        "valid": True,
        "source_complete": True,
        "candidate_package_complete": True,
        "registry_version": candidate.get("registry_version", ""),
        "resource_hashes": expected_registry_hashes,
        "component_hashes": component_hashes,
        "candidate_count": int(candidate.get("candidate_count") or 0),
        "watch_candidate_count": int(candidate.get("watch_candidate_count") or 0),
        "vector_layer_enabled": False,
        "prompt_character_count": int(prompt_package.get("prompt_character_count") or 0),
    }


def _validate_provider_health(
    health: dict[str, Any],
    *,
    expected_provider_version: str,
) -> list[str]:
    diagnostics = health.get("diagnostics") if isinstance(health, dict) else {}
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    errors = []
    expected = {
        "status": (health.get("status"), "ok"),
        "provider_server_version": (
            health.get("provider_server_version"),
            expected_provider_version,
        ),
        "server_mode": (health.get("server_mode"), "transformers_local"),
        "model": (Path(str(diagnostics.get("model_path") or "")).name, ENTITY_ALLOWED_MODEL),
        "allow_cpu_offload": (diagnostics.get("allow_cpu_offload"), False),
        "device_map_profile": (
            diagnostics.get("device_map_profile"),
            ENTITY_GPU_READINESS_PROFILE,
        ),
    }
    for name, (actual, wanted) in expected.items():
        if actual != wanted:
            errors.append(f"{name}={actual!r}; expected {wanted!r}")
    allocator = dict(diagnostics.get("cuda_allocator") or {})
    if allocator.get("configured_value") != "expandable_segments:True":
        errors.append("CUDA allocator is not configured for expandable segments")
    if allocator.get("torch_cuda_support_confirmed") is not True:
        errors.append("Torch 2.5.1+cu121 allocator support is not confirmed")
    if diagnostics.get("post_load_gate_contract") != "snapshot-complete-v2":
        errors.append("post-load snapshot contract is not v2")
    if diagnostics.get("generation_ready_gate_contract") != "token-aware-kv-v1":
        errors.append("token-aware generation gate contract is unavailable")
    if int(diagnostics.get("generation_runtime_margin_mib") or 0) < ENTITY_GPU_READINESS_RUNTIME_MARGIN_MIB:
        errors.append("generation runtime margin is below 4096 MiB")
    return errors


def _validate_remote_tokenization(
    *,
    tokenization: Any,
    local_gate: dict[str, Any],
    prompt_fingerprint: dict[str, str],
) -> list[str]:
    errors = []
    if tokenization.status != ExecutionStatus.COMPLETED.value:
        errors.append(f"tokenizer status={tokenization.status!r}")
    if tokenization.provider != ENTITY_ALLOWED_PROVIDER:
        errors.append(f"tokenizer provider={tokenization.provider!r}")
    if tokenization.model != ENTITY_ALLOWED_MODEL:
        errors.append(f"tokenizer model={tokenization.model!r}")
    payload = dict(tokenization.payload or {})
    validation = dict(tokenization.validation or {})
    provenance = dict(tokenization.provenance or {})
    counts = dict(payload.get("token_counts") or {})
    expected = {
        "max_output_tokens": ENTITY_MAX_OUTPUT_TOKENS,
        "model_context_limit": ENTITY_CONTEXT_LIMIT,
        "target_prompt_tokens": ENTITY_TARGET_PROMPT_TOKENS,
        "required_input_safety_margin_tokens": ENTITY_INPUT_SAFETY_MARGIN_TOKENS,
    }
    for key, wanted in expected.items():
        if int(counts.get(key) or 0) != wanted:
            errors.append(f"{key}={counts.get(key)!r}; expected {wanted}")
    prompt_tokens = int(counts.get("prompt_tokens") or 0)
    if not 0 < prompt_tokens <= ENTITY_TARGET_PROMPT_TOKENS:
        errors.append(f"prompt_tokens={prompt_tokens} exceeds the verified target")
    if int(counts.get("actual_input_safety_margin_tokens") or 0) < ENTITY_INPUT_SAFETY_MARGIN_TOKENS:
        errors.append("actual tokenizer safety margin is below 1024 tokens")
    for key in (
        "prompt_integrity_valid",
        "context_limit_respected",
        "target_prompt_budget_respected",
        "required_input_safety_margin_respected",
    ):
        if validation.get(key) is not True:
            errors.append(f"tokenizer validation {key} is not true")
    if validation.get("prompt_truncated") is not False:
        errors.append("tokenizer reported prompt truncation")
    if validation.get("generation_enabled") is not False:
        errors.append("tokenizer preflight did not prove generation disabled")
    if provenance.get("model_call_attempted") is not False:
        errors.append("tokenizer preflight attempted a model call")
    if provenance.get("model_loaded_for_request") is not False:
        errors.append("tokenizer preflight loaded the model")
    prompt_evidence = dict(payload.get("prompt") or {})
    if prompt_evidence.get("source_complete") is not True:
        errors.append("remote prompt integrity did not preserve source_complete")
    comparison = dict(payload.get("component_comparison") or {})
    current = dict(comparison.get("current") or {})
    if current.get("label") != "entity-bounded-knowledge-v2":
        errors.append("remote tokenizer did not report entity-bounded-knowledge-v2")
    if int(current.get("prompt_tokens") or 0) != prompt_tokens:
        errors.append("component comparison prompt token total differs from tokenizer total")
    remote_hashes = {
        str(item.get("name") or ""): str(item.get("sha256") or "")
        for item in current.get("components") or []
        if isinstance(item, dict)
    }
    if remote_hashes != local_gate.get("component_hashes"):
        errors.append("remote tokenizer component hashes differ from the final local prompt")
    if not all(prompt_fingerprint.values()):
        errors.append("final prompt fingerprint is incomplete")
    return errors


def _build_execution_result(
    *,
    response: Any,
    request: dict[str, Any],
    document: Document,
    prompt_package: dict[str, Any],
    prompt_fingerprint: dict[str, str],
    health: dict[str, Any],
    tokenization: Any,
) -> StageExecutionResult:
    provider_provenance = dict((response.metadata or {}).get("provider_provenance") or {})
    provider_envelope_diagnostics = dict(
        (response.payload or {}).get("diagnostics") or {}
    )
    nested_execution = provider_envelope_diagnostics.get("execution_diagnostics")
    provider_diagnostics = (
        dict(nested_execution)
        if isinstance(nested_execution, dict)
        else dict(provider_envelope_diagnostics)
    )
    post_load_gate = _gate_evidence_or_unavailable(
        provider_diagnostics.get("post_load_vram_gate"),
        gate="post_load_vram_gate",
    )
    generation_ready_gate = _gate_evidence_or_unavailable(
        provider_diagnostics.get("generation_ready_vram_gate"),
        gate="generation_ready_vram_gate",
    )
    generation_gate_cleanup = dict(
        provider_diagnostics.get("generation_ready_vram_gate_cleanup") or {}
    )
    completion = dict(provider_diagnostics.get("completion_evidence") or {})
    client_metadata = dict(response.metadata or {})
    attempted_evidence = provider_provenance.get("model_call_attempted")
    if not isinstance(attempted_evidence, bool):
        attempted_evidence = response.validation.get("model_call_attempted")
    if not isinstance(attempted_evidence, bool):
        attempted_evidence = None
    completed_evidence = provider_provenance.get("model_call_completed")
    if not isinstance(completed_evidence, bool):
        completed_evidence = response.validation.get("model_call_completed")
    if not isinstance(completed_evidence, bool):
        completed_evidence = None
    errors = list(response.errors or [])
    warnings = list(response.warnings or [])
    payload: dict[str, Any] = {
        "provider_payload": dict(response.payload or {}),
    }
    entity_validation: dict[str, Any]
    status = str(response.status or ExecutionStatus.ERROR.value)
    if status == ExecutionStatus.COMPLETED.value:
        evidence_checks = {
            "unexpected_provider": response.provider == ENTITY_ALLOWED_PROVIDER,
            "unexpected_model": response.model == ENTITY_ALLOWED_MODEL,
            "real_execution_evidence_missing": response.real_chatbot_execution is True,
            "model_call_attempt_evidence_missing": attempted_evidence is True,
            "model_call_completion_evidence_missing": completed_evidence is True,
            "prompt_truncation_evidence_invalid": response.validation.get("prompt_truncated") is False,
            "source_completeness_evidence_invalid": response.validation.get("source_complete") is True,
            "generation_completion_evidence_invalid": completion.get("generation_may_be_truncated") is False,
            "post_load_vram_gate_evidence_invalid": (
                post_load_gate.get("passed") is True
            ),
            "generation_ready_vram_gate_evidence_invalid": (
                generation_ready_gate.get("passed") is True
            ),
            "generation_ready_vram_gate_cleanup_evidence_invalid": (
                generation_gate_cleanup.get("passed") is True
            ),
        }
        errors.extend(
            {
                "code": code,
                "message": f"Controlled generation completion gate failed: {code}.",
            }
            for code, passed in evidence_checks.items()
            if not passed
        )
    if (
        status == ExecutionStatus.COMPLETED.value
        and response.provider == ENTITY_ALLOWED_PROVIDER
        and response.model == ENTITY_ALLOWED_MODEL
        and response.real_chatbot_execution
        and attempted_evidence is True
        and completed_evidence is True
        and response.validation.get("prompt_truncated") is False
        and response.validation.get("source_complete") is True
        and completion.get("generation_may_be_truncated") is False
        and post_load_gate.get("passed") is True
        and generation_ready_gate.get("passed") is True
        and generation_gate_cleanup.get("passed") is True
        and not errors
    ):
        header, body = _document_header_and_body(document)
        try:
            parsed, validation = parse_and_validate_entity_output(
                response.raw_output,
                expected_header=header,
                source_body=body,
            )
            entity_validation = validation.as_dict()
            payload["entity_output"] = parsed.as_dict()
            if completion.get("generation_may_be_truncated") is True:
                entity_validation = {
                    **entity_validation,
                    "valid": False,
                    "issues": [
                        *list(entity_validation.get("issues") or []),
                        {
                            "code": "generation_may_be_truncated",
                            "message": "Generation hit the output ceiling without EOS evidence.",
                            "severity": "error",
                        },
                    ],
                }
            if not entity_validation.get("valid"):
                status = ExecutionStatus.VALIDATION_FAILED.value
        except Exception as exc:
            status = ExecutionStatus.VALIDATION_FAILED.value
            entity_validation = _invalid_validation(
                [{"code": "entity_output_malformed", "message": str(exc)}]
            )
            errors.append(
                {"code": "entity_output_malformed", "message": str(exc)}
            )
    else:
        entity_validation = _invalid_validation(
            errors
            or [
                {
                    "code": "entity_provider_generation_failed",
                    "message": response.error or "Provider did not complete real Entity generation.",
                }
            ]
        )
        if status == ExecutionStatus.COMPLETED.value:
            status = ExecutionStatus.VALIDATION_FAILED.value
    payload["entity_validation"] = entity_validation
    provenance = {
        "contract_version": ENTITY_REAL_GENERATION_CONTRACT,
        "request": request,
        "provider": response.provider or ENTITY_ALLOWED_PROVIDER,
        "model": response.model or ENTITY_ALLOWED_MODEL,
        "real_chatbot_execution": bool(response.real_chatbot_execution),
        "model_call_attempted": attempted_evidence,
        "model_call_completed": completed_evidence,
        "prompt_fingerprint": prompt_fingerprint,
        "provider_health": _redacted_health(health),
        "tokenizer_preflight": _tokenization_audit(tokenization),
        "provider_response": {
            "validation": dict(response.validation or {}),
            "provenance": provider_provenance,
            "timing": dict(provider_diagnostics.get("timing") or {}),
            "token_counts": dict(provider_diagnostics.get("token_counts") or {}),
            "completion_evidence": completion,
            "post_load_vram_gate": post_load_gate,
            "generation_ready_vram_gate": generation_ready_gate,
            "generation_ready_vram_gate_cleanup": dict(
                generation_gate_cleanup
            ),
            "memory_snapshot_at_failure": list(
                provider_diagnostics.get("memory_snapshot_at_failure")
                or provider_diagnostics.get("memory_after_exception")
                or []
            ),
            "placement": dict(provider_diagnostics.get("placement") or {}),
            "cuda_cache_cleanup": dict(
                provider_diagnostics.get("cuda_cache_cleanup") or {}
            ),
            "request_lifecycle": dict(provider_provenance.get("request_lifecycle") or {}),
            "runtime_contract": _provider_runtime_contract(
                provider_envelope_diagnostics,
                provider_provenance,
            ),
            "client_metadata": client_metadata,
        },
        "bundle": _bundle_from_prompt_package(prompt_package),
        "warnings": warnings,
        "errors": errors,
    }
    return StageExecutionResult(
        status=status,
        raw_output=str(response.raw_output or ""),
        payload=payload,
        provenance=provenance,
        error=str(response.error or ""),
        provider=str(response.provider or ENTITY_ALLOWED_PROVIDER),
        model=str(response.model or ENTITY_ALLOWED_MODEL),
        validation={"entity_output": entity_validation},
        warnings=warnings,
        errors=errors,
        request=request,
    )


def _provider_runtime_contract(
    envelope_diagnostics: dict[str, Any],
    provider_provenance: dict[str, Any],
) -> dict[str, Any]:
    model_config = provider_provenance.get("model_config")
    config = dict(model_config) if isinstance(model_config, dict) else {}
    if not config.get("provider_server_version"):
        config = {**dict(envelope_diagnostics), **config}
    return {
        "provider_server_version": config.get("provider_server_version"),
        "device_map_profile": config.get("device_map_profile"),
        "vram_reserve_gib": config.get("vram_reserve_gib"),
        "generation_runtime_margin_mib": config.get(
            "generation_runtime_margin_mib"
        ),
        "allocator": dict(config.get("cuda_allocator") or {}),
        "post_load_gate_contract": config.get("post_load_gate_contract"),
        "generation_ready_gate_contract": config.get(
            "generation_ready_gate_contract"
        ),
    }


def _gate_evidence_or_unavailable(value: Any, *, gate: str) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        return dict(value)
    return {
        "passed": False,
        "status": "unavailable",
        "reason": "provider_gate_evidence_unavailable",
        "gate": gate,
        "model_load_completed": None,
    }


def _invalid_validation(errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "valid": False,
        "requires_human_review": False,
        "tag_count": 0,
        "issues": list(errors),
        "registry_version": "",
        "resource_hashes": {},
    }


def _build_summary(
    *,
    result: StageExecutionResult,
    persistence: dict[str, Any],
    request_id: str,
    prompt_package: dict[str, Any],
    tokenization: Any,
    document: Document,
    stage_output: StageOutput,
) -> dict[str, Any]:
    validation = dict(result.payload.get("entity_validation") or {})
    candidate = dict(prompt_package.get("entity_candidate_package") or {})
    preflight = _tokenization_audit(tokenization)
    provider_response = dict(result.provenance.get("provider_response") or {})
    token_counts = dict(provider_response.get("token_counts") or {})
    completion = dict(provider_response.get("completion_evidence") or {})
    attempted = result.provenance.get("model_call_attempted")
    completed = result.provenance.get("model_call_completed")
    lifecycle_status = (
        StageOutput.Status.CHECKING
        if persistence.get("applied_to_stage_output")
        else "not_applied"
    )
    human_review_reasons = [
        item
        for item in validation.get("issues") or []
        if isinstance(item, dict) and item.get("severity") == "human_review"
    ]
    return {
        "contract_version": ENTITY_REAL_GENERATION_CONTRACT,
        "request_id": request_id,
        "attempt_id": persistence.get("attempt_id"),
        "stage_output_id": persistence.get("stage_output_id"),
        "execution_status": result.status,
        "lifecycle_status": lifecycle_status,
        "real_chatbot_execution": bool(result.provenance.get("real_chatbot_execution")),
        "model_call_attempted": attempted,
        "model_call_completed": completed,
        "prompt_tokens": preflight.get("prompt_tokens"),
        "generated_tokens": token_counts.get("generated_tokens"),
        "max_output_tokens": ENTITY_MAX_OUTPUT_TOKENS,
        "finish_evidence": completion,
        "raw_output_character_count": len(result.raw_output),
        "parsed_entity_tag_count": int(validation.get("tag_count") or 0),
        "deterministic_validation_valid": bool(validation.get("valid")),
        "requires_human_review": bool(validation.get("requires_human_review")),
        "human_review_reasons": human_review_reasons,
        "source_complete": bool(persistence.get("source_complete")),
        "registry_version": candidate.get("registry_version", ""),
        "registry_resource_hashes": candidate.get("resource_hashes", {}),
        "candidate_count": int(candidate.get("candidate_count") or 0),
        "watch_candidate_count": int(candidate.get("watch_candidate_count") or 0),
        "provider_timing": provider_response.get("timing", {}),
        "post_load_vram_gate": provider_response.get("post_load_vram_gate", {}),
        "generation_ready_vram_gate": provider_response.get(
            "generation_ready_vram_gate", {}
        ),
        "generation_ready_vram_gate_cleanup": provider_response.get(
            "generation_ready_vram_gate_cleanup", {}
        ),
        "memory_snapshot_at_failure": provider_response.get(
            "memory_snapshot_at_failure", []
        ),
        "provider_runtime_contract": provider_response.get("runtime_contract", {}),
        "generation_may_be_truncated": completion.get("generation_may_be_truncated"),
        "errors": list(result.errors or []),
        "warnings": list(result.warnings or []),
        "database_changes": {
            "stage_execution_attempt_created": persistence.get("attempt_id"),
            "entity_stage_output_updated": (
                stage_output.pk if persistence.get("applied_to_stage_output") else None
            ),
            "entity_stage_output_fields": (
                ["payload", "raw_output", "provenance", "status"]
                if persistence.get("applied_to_stage_output")
                else []
            ),
            "document_modified": False,
            "clause_parser_stage_output_modified": False,
            "clauses_modified": False,
            "other_stage_outputs_modified": False,
            "document_id": document.doc_id,
        },
    }


def _prompt_fingerprint(prompt_package: dict[str, Any]) -> dict[str, str]:
    return {
        "system_prompt_sha256": hashlib.sha256(
            str(prompt_package.get("system_prompt") or "").encode("utf-8")
        ).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(
            str(prompt_package.get("user_prompt") or "").encode("utf-8")
        ).hexdigest(),
    }


def _bundle_from_prompt_package(prompt_package: dict[str, Any]) -> dict[str, Any]:
    return {
        "loaded_files": list(prompt_package.get("loaded_files") or []),
        "indexed_resources": list(prompt_package.get("indexed_resources") or []),
        "entity_candidate_package": dict(
            prompt_package.get("entity_candidate_package") or {}
        ),
        "prompt_component_summary": dict(
            prompt_package.get("prompt_component_summary") or {}
        ),
        "source_completeness": dict(prompt_package.get("source_completeness") or {}),
    }


def _tokenization_audit(tokenization: Any | None) -> dict[str, Any]:
    if tokenization is None:
        return {}
    payload = dict(getattr(tokenization, "payload", {}) or {})
    comparison = dict(payload.get("component_comparison") or {})
    return {
        "status": str(getattr(tokenization, "status", "") or ""),
        "provider": str(getattr(tokenization, "provider", "") or ""),
        "model": str(getattr(tokenization, "model", "") or ""),
        "prompt_tokens": (payload.get("token_counts") or {}).get("prompt_tokens"),
        "token_counts": dict(payload.get("token_counts") or {}),
        "validation": dict(getattr(tokenization, "validation", {}) or {}),
        "provenance": dict(getattr(tokenization, "provenance", {}) or {}),
        "component_comparison": comparison,
        "errors": list(getattr(tokenization, "errors", []) or []),
        "warnings": list(getattr(tokenization, "warnings", []) or []),
    }


def _redacted_health(health: dict[str, Any]) -> dict[str, Any]:
    diagnostics = dict(health.get("diagnostics") or {}) if isinstance(health, dict) else {}
    runtime = dict(diagnostics.get("runtime") or {})
    return {
        "status": health.get("status") if isinstance(health, dict) else None,
        "provider_server_version": (
            health.get("provider_server_version") if isinstance(health, dict) else None
        ),
        "server_mode": health.get("server_mode") if isinstance(health, dict) else None,
        "model_path_basename": Path(str(diagnostics.get("model_path") or "")).name,
        "device_map_profile": diagnostics.get("device_map_profile"),
        "allow_cpu_offload": diagnostics.get("allow_cpu_offload"),
        "vram_reserve_gib": diagnostics.get("vram_reserve_gib"),
        "generation_runtime_margin_mib": diagnostics.get(
            "generation_runtime_margin_mib"
        ),
        "post_load_gate_contract": diagnostics.get("post_load_gate_contract"),
        "generation_ready_gate_contract": diagnostics.get(
            "generation_ready_gate_contract"
        ),
        "cuda_allocator": dict(diagnostics.get("cuda_allocator") or {}),
        "model_loaded": runtime.get("model_loaded"),
        "readiness_evidence": dict(runtime.get("readiness_evidence") or {}),
    }
