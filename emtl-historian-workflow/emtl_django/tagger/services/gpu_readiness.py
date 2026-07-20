from __future__ import annotations

from typing import Any
from uuid import uuid4

from .providers.gpu_local import (
    GpuLocalProviderClient,
    GpuReadinessDiagnosticResult,
)


ENTITY_GPU_READINESS_PROFILE = "qwen2_5_32b_long_context_v2"
ENTITY_GPU_READINESS_MAX_OUTPUT_TOKENS = 4096
ENTITY_GPU_READINESS_RUNTIME_MARGIN_MIB = 4096
LOAD_ONLY_OPERATION = "load_only"
SYNTHETIC_OPERATION = "synthetic_long_context"


def run_readiness_diagnostic(
    *,
    operation: str,
    prompt_tokens: int,
    client: GpuLocalProviderClient | None = None,
    request_id: str = "",
) -> GpuReadinessDiagnosticResult:
    if operation not in {LOAD_ONLY_OPERATION, SYNTHETIC_OPERATION}:
        raise ValueError(f"Unsupported readiness operation: {operation}")
    if int(prompt_tokens) < 1:
        raise ValueError("prompt_tokens must be positive")
    active_client = client or GpuLocalProviderClient()
    return active_client.readiness_diagnostic(
        operation=operation,
        request_id=request_id.strip() or f"stage18a-{operation}-{uuid4().hex}",
        device_map_profile=ENTITY_GPU_READINESS_PROFILE,
        prompt_tokens=int(prompt_tokens),
        max_output_tokens=ENTITY_GPU_READINESS_MAX_OUTPUT_TOKENS,
    )


def readiness_summary(result: GpuReadinessDiagnosticResult) -> dict[str, Any]:
    diagnostics = dict(result.payload.get("diagnostics") or {})
    return {
        "operation": result.operation,
        "status": result.status,
        "passed": result.passed,
        "provider_server_version": result.provider_server_version,
        "model": result.model,
        "profile": result.provenance.get("device_map_profile"),
        "prompt_tokens": diagnostics.get("prompt_tokens"),
        "max_output_tokens": diagnostics.get("max_output_tokens"),
        "post_load_vram_gate": diagnostics.get("post_load_vram_gate", {}),
        "generation_ready_vram_gate": diagnostics.get(
            "generation_ready_vram_gate", {}
        ),
        "generation_ready_vram_gate_cleanup": diagnostics.get(
            "generation_ready_vram_gate_cleanup", {}
        ),
        "post_diagnostic_cleanup_ran": diagnostics.get(
            "post_diagnostic_cleanup_ran"
        ),
        "post_synthetic_cleanup": diagnostics.get(
            "post_synthetic_cleanup", {}
        ),
        "post_cleanup_generation_ready_vram_gate": diagnostics.get(
            "post_cleanup_generation_ready_vram_gate", {}
        ),
        "model_call_attempted": diagnostics.get("model_call_attempted"),
        "model_call_completed": diagnostics.get("model_call_completed"),
        "business_data_used": diagnostics.get("business_data_used"),
        "synthetic_contract": diagnostics.get("synthetic_contract", {}),
        "memory_snapshot_at_failure": diagnostics.get(
            "memory_snapshot_at_failure", []
        ),
        "cuda_cache_cleanup": diagnostics.get("cuda_cache_cleanup", {}),
        "errors": result.errors,
    }


def validate_latest_readiness_evidence(
    health: dict[str, Any],
    *,
    expected_provider_version: str,
    prompt_tokens: int,
) -> list[str]:
    diagnostics = dict(health.get("diagnostics") or {})
    runtime = dict(diagnostics.get("runtime") or {})
    evidence = dict(runtime.get("readiness_evidence") or {})
    errors: list[str] = []
    for operation in (LOAD_ONLY_OPERATION, SYNTHETIC_OPERATION):
        item = dict(evidence.get(operation) or {})
        prefix = f"readiness {operation}"
        expected = {
            "status": (item.get("status"), "completed"),
            "passed": (item.get("passed"), True),
            "provider_server_version": (
                item.get("provider_server_version"),
                expected_provider_version,
            ),
            "device_map_profile": (
                item.get("device_map_profile"),
                ENTITY_GPU_READINESS_PROFILE,
            ),
            "prompt_tokens": (item.get("prompt_tokens"), int(prompt_tokens)),
            "max_output_tokens": (
                item.get("max_output_tokens"),
                ENTITY_GPU_READINESS_MAX_OUTPUT_TOKENS,
            ),
            "business_data_used": (item.get("business_data_used"), False),
        }
        for name, (actual, wanted) in expected.items():
            if actual != wanted:
                errors.append(f"{prefix} {name}={actual!r}; expected {wanted!r}")
        post_load = dict(item.get("post_load_vram_gate") or {})
        generation = dict(item.get("generation_ready_vram_gate") or {})
        gate_cleanup = dict(
            item.get("generation_ready_vram_gate_cleanup") or {}
        )
        if post_load.get("passed") is not True:
            errors.append(f"{prefix} post-load gate did not pass")
        if generation.get("passed") is not True:
            errors.append(f"{prefix} token-aware generation gate did not pass")
        if gate_cleanup.get("passed") is not True:
            errors.append(f"{prefix} pre-gate CUDA cache cleanup did not pass")
        if generation.get("same_process_reclaimable_estimate_used_for_decision") is not False:
            errors.append(f"{prefix} gate did not prove actual-free-only decision")
        if int(generation.get("runtime_margin_mib") or 0) < ENTITY_GPU_READINESS_RUNTIME_MARGIN_MIB:
            errors.append(f"{prefix} runtime margin is below 4096 MiB")
        if operation == LOAD_ONLY_OPERATION:
            if item.get("model_call_attempted") is not False:
                errors.append("load-only readiness attempted generation")
        else:
            if item.get("model_call_attempted") is not True:
                errors.append("synthetic readiness did not attempt its one-token generation")
            if item.get("model_call_completed") is not True:
                errors.append("synthetic readiness did not complete its one-token generation")
            if item.get("post_diagnostic_cleanup_ran") is not True:
                errors.append("synthetic readiness did not run post-diagnostic cleanup")
            post_cleanup = dict(item.get("post_synthetic_cleanup") or {})
            post_cleanup_gate = dict(
                item.get("post_cleanup_generation_ready_vram_gate") or {}
            )
            if post_cleanup.get("passed") is not True:
                errors.append("synthetic post-diagnostic cache cleanup did not pass")
            if post_cleanup_gate.get("passed") is not True:
                errors.append("synthetic post-cleanup generation gate did not pass")
            if post_cleanup_gate != generation:
                errors.append(
                    "synthetic readiness evidence is not the post-cleanup gate"
                )
    return errors
