from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import requests

from ..contracts import (
    ExecutionStatus,
    ProviderLabel,
    STAGE_CONTRACT_VERSION,
    STAGE_EXECUTION_RESULT_SCHEMA,
    normalize_provider_label,
)


DEFAULT_GPU_LOCAL_TIMEOUT = 3600


@dataclass(frozen=True)
class GpuLocalProviderResult:
    status: str
    raw_output: str = ""
    provider: str = ProviderLabel.GPU_LOCAL.value
    model: str = ""
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    real_chatbot_execution: bool = False


@dataclass(frozen=True)
class GpuTokenizationPreflightResult:
    status: str
    provider: str = ProviderLabel.GPU_LOCAL.value
    model: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class GpuProviderHealthResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class GpuReadinessDiagnosticResult:
    status: str
    operation: str
    passed: bool
    provider_server_version: str = ""
    model: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class GpuLocalProviderClient:
    provider_name = ProviderLabel.GPU_LOCAL.value

    def __init__(self, endpoint: str | None = None, timeout: int | None = None) -> None:
        self.endpoint = endpoint or os.getenv("EMTL_GPU_LOCAL_URL", "")
        self.timeout = int(
            os.getenv("EMTL_GPU_LOCAL_TIMEOUT_SECONDS")
            or timeout
            or DEFAULT_GPU_LOCAL_TIMEOUT
        )

    def generate(self, provider_payload: dict[str, Any]) -> GpuLocalProviderResult:
        if not self.endpoint.strip():
            return self._failure(
                status=ExecutionStatus.UNAVAILABLE.value,
                code="gpu_local_url_not_configured",
                message="EMTL_GPU_LOCAL_URL is not configured.",
            )
        try:
            response = requests.post(
                self.endpoint,
                json=provider_payload,
                timeout=(5, self.timeout),
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            request_id = str(provider_payload.get("request_id") or "")
            stage_id = str(provider_payload.get("stage_id") or "")
            return self._failure(
                status=ExecutionStatus.UNAVAILABLE.value,
                code="gpu_local_timeout",
                message=(
                    "The local client timed out while waiting for gpu_local; the remote "
                    "request may still be queued or running, so model completion is unknown: "
                    f"{exc}"
                ),
                metadata={
                    "request_lifecycle": {
                        "state": "timed_out",
                        "request_id": request_id,
                        "stage_id": stage_id,
                        "client_timeout_seconds": self.timeout,
                        "remote_completion_known": False,
                    }
                },
            )
        except requests.RequestException as exc:
            request_id = str(provider_payload.get("request_id") or "")
            stage_id = str(provider_payload.get("stage_id") or "")
            return self._failure(
                status=ExecutionStatus.UNAVAILABLE.value,
                code="gpu_local_unavailable",
                message=(
                    "The gpu_local connection failed; if the request was submitted, its "
                    f"remote completion state is unknown: {exc}"
                ),
                metadata={
                    "request_lifecycle": {
                        "state": "failed",
                        "request_id": request_id,
                        "stage_id": stage_id,
                        "remote_completion_known": False,
                    }
                },
            )

        try:
            data = response.json()
        except ValueError as exc:
            return self._failure(
                status=ExecutionStatus.ERROR.value,
                code="malformed_provider_response",
                message=f"gpu_local provider returned non-JSON response: {exc}",
            )
        if not isinstance(data, dict):
            return self._failure(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                code="invalid_provider_response",
                message="gpu_local provider response must be a JSON object.",
            )
        return self._normalize_response(data)

    def tokenize_only(self, provider_payload: dict[str, Any]) -> GpuTokenizationPreflightResult:
        if not self.endpoint.strip():
            return GpuTokenizationPreflightResult(
                status=ExecutionStatus.UNAVAILABLE.value,
                error="EMTL_GPU_LOCAL_URL is not configured.",
                errors=[
                    {
                        "code": "gpu_local_url_not_configured",
                        "message": "EMTL_GPU_LOCAL_URL is not configured.",
                    }
                ],
            )
        endpoint = _tokenization_endpoint(self.endpoint)
        try:
            response = requests.post(
                endpoint,
                json=provider_payload,
                timeout=(5, self.timeout),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            return GpuTokenizationPreflightResult(
                status=ExecutionStatus.UNAVAILABLE.value,
                error=str(exc),
                errors=[{"code": "gpu_tokenization_preflight_unavailable", "message": str(exc)}],
            )
        if not isinstance(data, dict):
            return GpuTokenizationPreflightResult(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                error="Tokenization preflight response must be a JSON object.",
                errors=[
                    {
                        "code": "invalid_tokenization_preflight_response",
                        "message": "Tokenization preflight response must be a JSON object.",
                    }
                ],
            )
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
        provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        errors = [item for item in data.get("errors", []) if isinstance(item, dict)] if isinstance(data.get("errors"), list) else []
        warnings = [str(item) for item in data.get("warnings", [])] if isinstance(data.get("warnings"), list) else []
        status = str(data.get("status") or "")
        if payload.get("operation") != "tokenization_only" or validation.get("generation_enabled") is not False:
            return GpuTokenizationPreflightResult(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                error="Remote response did not prove tokenization-only mode with generation disabled.",
                errors=[
                    {
                        "code": "tokenization_only_proof_missing",
                        "message": "Remote response did not prove tokenization-only mode with generation disabled.",
                    }
                ],
            )
        return GpuTokenizationPreflightResult(
            status=status,
            provider=str(data.get("provider") or self.provider_name),
            model=str(data.get("model") or ""),
            payload=payload,
            validation=validation,
            provenance=provenance,
            warnings=warnings,
            errors=errors,
            error=_first_error_message(errors),
        )

    def health(self) -> GpuProviderHealthResult:
        if not self.endpoint.strip():
            return GpuProviderHealthResult(
                ok=False,
                error="EMTL_GPU_LOCAL_URL is not configured.",
            )
        try:
            response = requests.get(
                _health_endpoint(self.endpoint),
                timeout=(5, 15),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            return GpuProviderHealthResult(ok=False, error=str(exc))
        if not isinstance(data, dict) or data.get("status") != "ok":
            return GpuProviderHealthResult(
                ok=False,
                payload=data if isinstance(data, dict) else {},
                error="GPU provider health response is not an ok object.",
            )
        return GpuProviderHealthResult(ok=True, payload=data)

    def readiness_diagnostic(
        self,
        *,
        operation: str,
        request_id: str,
        device_map_profile: str,
        prompt_tokens: int,
        max_output_tokens: int,
    ) -> GpuReadinessDiagnosticResult:
        if not self.endpoint.strip():
            return GpuReadinessDiagnosticResult(
                status=ExecutionStatus.UNAVAILABLE.value,
                operation=operation,
                passed=False,
                error="EMTL_GPU_LOCAL_URL is not configured.",
                errors=[
                    {
                        "code": "gpu_local_url_not_configured",
                        "message": "EMTL_GPU_LOCAL_URL is not configured.",
                    }
                ],
            )
        endpoint = _readiness_endpoint(self.endpoint, operation)
        request_payload = {
            "schema_version": "emtl-gpu-readiness-request-v1",
            "contract_version": STAGE_CONTRACT_VERSION,
            "request_id": request_id,
            "stage_id": "entity_registry_readiness",
            "operation": operation,
            "device_map_profile": device_map_profile,
            "prompt_tokens": int(prompt_tokens),
            "max_output_tokens": int(max_output_tokens),
        }
        try:
            response = requests.post(
                endpoint,
                json=request_payload,
                timeout=(5, self.timeout),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            return GpuReadinessDiagnosticResult(
                status=ExecutionStatus.UNAVAILABLE.value,
                operation=operation,
                passed=False,
                error=str(exc),
                errors=[
                    {
                        "code": "gpu_readiness_diagnostic_unavailable",
                        "message": str(exc),
                    }
                ],
            )
        if not isinstance(data, dict):
            return GpuReadinessDiagnosticResult(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                operation=operation,
                passed=False,
                error="Readiness diagnostic response must be a JSON object.",
            )
        errors = [
            item
            for item in (data.get("errors") or [])
            if isinstance(item, dict)
        ]
        response_operation = str(data.get("operation") or "")
        passed = bool(data.get("passed"))
        validation = dict(data.get("validation") or {})
        if response_operation != operation or validation.get("business_data_used") is not False:
            errors.append(
                {
                    "code": "gpu_readiness_contract_invalid",
                    "message": "Diagnostic operation or non-business proof is invalid.",
                }
            )
            passed = False
        return GpuReadinessDiagnosticResult(
            status=str(data.get("status") or ""),
            operation=response_operation,
            passed=passed,
            provider_server_version=str(data.get("provider_server_version") or ""),
            model=str(data.get("model") or ""),
            payload=dict(data.get("payload") or {}),
            validation=validation,
            provenance=dict(data.get("provenance") or {}),
            errors=errors,
            error=_first_error_message(errors),
        )

    def _normalize_response(self, data: dict[str, Any]) -> GpuLocalProviderResult:
        status = str(data.get("status") or "").strip()
        if status not in {
            ExecutionStatus.COMPLETED.value,
            ExecutionStatus.UNAVAILABLE.value,
            ExecutionStatus.BLOCKED.value,
            ExecutionStatus.VALIDATION_FAILED.value,
            ExecutionStatus.ERROR.value,
        }:
            return self._failure(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                code="invalid_provider_status",
                message=f"Unsupported gpu_local provider status: {status or '<empty>'}",
                metadata={"provider_response": data},
            )
        try:
            provider = normalize_provider_label(str(data.get("provider") or self.provider_name))
        except ValueError:
            return self._failure(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                code="invalid_provider_label",
                message=f"Unsupported gpu_local provider label: {data.get('provider')}",
                metadata={"provider_response": data},
            )
        if provider != self.provider_name:
            return self._failure(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                code="unexpected_provider_label",
                message=f"Expected gpu_local provider response, received {provider}.",
                metadata={"provider_response": data},
            )
        raw_output = str(data.get("raw_output") or "")
        if status == ExecutionStatus.COMPLETED.value and not raw_output.strip():
            return self._failure(
                status=ExecutionStatus.VALIDATION_FAILED.value,
                code="missing_raw_output",
                message="Completed gpu_local response did not include raw_output.",
                metadata={"provider_response": data},
            )
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
        provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        raw_warnings = data.get("warnings", [])
        if not isinstance(raw_warnings, list):
            raw_warnings = [raw_warnings]
        warnings = [str(item) for item in raw_warnings if str(item).strip()]
        raw_errors = data.get("errors", [])
        if not isinstance(raw_errors, list):
            raw_errors = []
        errors = [
            item
            for item in raw_errors
            if isinstance(item, dict)
        ]
        model = str(data.get("model") or provenance.get("model") or "gpu-local-provider")
        return GpuLocalProviderResult(
            status=status,
            raw_output=raw_output,
            provider=provider,
            model=model,
            error=_first_error_message(errors),
            payload=payload,
            validation=validation,
            warnings=warnings,
            errors=errors,
            metadata={
                "provider_response_schema": data.get("schema_version", ""),
                "provider_contract_version": data.get("contract_version", ""),
                "provider_payload": payload,
                "provider_provenance": provenance,
            },
            real_chatbot_execution=bool(provenance.get("real_chatbot_execution", False)),
        )

    def _failure(
        self,
        *,
        status: str,
        code: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> GpuLocalProviderResult:
        return GpuLocalProviderResult(
            status=status,
            raw_output="",
            provider=self.provider_name,
            model="not-configured",
            error=message,
            errors=[{"code": code, "message": message}],
            metadata={
                "provider_response_schema": STAGE_EXECUTION_RESULT_SCHEMA,
                "provider_contract_version": STAGE_CONTRACT_VERSION,
                "endpoint": self.endpoint,
                **(metadata or {}),
            },
            real_chatbot_execution=False,
        )


def _first_error_message(errors: list[dict[str, Any]]) -> str:
    for error in errors:
        message = str(error.get("message") or "").strip()
        if message:
            return message
    return ""


def _tokenization_endpoint(endpoint: str) -> str:
    cleaned = str(endpoint or "").rstrip("/")
    if cleaned.endswith("/generate"):
        return cleaned[: -len("/generate")] + "/tokenize"
    return cleaned + "/tokenize"


def _health_endpoint(endpoint: str) -> str:
    cleaned = str(endpoint or "").rstrip("/")
    for suffix in ("/generate", "/tokenize"):
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)] + "/health"
    return cleaned + "/health"


def _readiness_endpoint(endpoint: str, operation: str) -> str:
    cleaned = str(endpoint or "").rstrip("/")
    for suffix in ("/generate", "/tokenize"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    paths = {
        "load_only": "/diagnostics/load-only",
        "synthetic_long_context": "/diagnostics/long-context-synthetic",
    }
    if operation not in paths:
        raise ValueError(f"Unsupported readiness diagnostic operation: {operation}")
    return cleaned + paths[operation]
