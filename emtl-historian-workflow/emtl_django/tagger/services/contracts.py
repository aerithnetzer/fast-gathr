from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


STAGE_CONTRACT_VERSION = "emtl-stage-contract-v1"
STAGE_EXECUTION_REQUEST_SCHEMA = "emtl-stage-execution-request-v1"
STAGE_EXECUTION_RESULT_SCHEMA = "emtl-stage-execution-result-v1"
STAGE_PROVIDER_API_PAYLOAD_SCHEMA = "emtl-provider-api-payload-draft-v1"


class StageId(str, Enum):
    SUMMARY_KEYWORDS = "summary_keywords"
    ENTITY_REGISTRY = "entity_registry"
    CLAUSE_PARSER = "clause_parser"
    OCCURRENCES_REGISTRY = "occurrences_registry"
    TAG_ASSEMBLER = "tag_assembler"
    KEY_NARRATIVE = "key_narrative"


STABLE_STAGE_IDS = tuple(stage.value for stage in StageId)


class ProviderLabel(str, Enum):
    FIXTURE = "fixture"
    BACKEND_STUB = "backend_stub"
    LOCAL_CPU = "local_cpu"
    GPU_LOCAL = "gpu_local"
    AWS_BEDROCK = "aws_bedrock"
    EXTERNAL_API = "external_api"


PROVIDER_LABELS = tuple(provider.value for provider in ProviderLabel)

PROVIDER_ALIASES = {
    "api": ProviderLabel.BACKEND_STUB.value,
    "api_stub": ProviderLabel.BACKEND_STUB.value,
    "backend_stub": ProviderLabel.BACKEND_STUB.value,
    "fixture": ProviderLabel.FIXTURE.value,
    "local": ProviderLabel.LOCAL_CPU.value,
    "local_cpu": ProviderLabel.LOCAL_CPU.value,
    "local_ollama": ProviderLabel.LOCAL_CPU.value,
    "gpu": ProviderLabel.GPU_LOCAL.value,
    "gpu_local": ProviderLabel.GPU_LOCAL.value,
    "aws": ProviderLabel.AWS_BEDROCK.value,
    "aws_bedrock": ProviderLabel.AWS_BEDROCK.value,
    "external": ProviderLabel.EXTERNAL_API.value,
    "external_api": ProviderLabel.EXTERNAL_API.value,
}


class ExecutionStatus(str, Enum):
    FIXTURE = "fixture"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    VALIDATION_FAILED = "validation_failed"
    ERROR = "error"


EXECUTION_STATUSES = tuple(status.value for status in ExecutionStatus)


class StageLifecycleStatus(str, Enum):
    NOT_STARTED = "not_started"
    LOADED = "loaded"
    CHECKING = "checking"
    ACCEPTED = "accepted"
    NEEDS_RERUN = "needs_rerun"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    CANCELLED = "cancelled"


STAGE_LIFECYCLE_STATUSES = tuple(status.value for status in StageLifecycleStatus)


PROVIDER_CAPABILITIES: dict[str, dict[str, Any]] = {
    ProviderLabel.FIXTURE.value: {
        "display_label": "Fixture provider",
        "real_chatbot_execution": False,
        "implemented": True,
        "supports_network": False,
    },
    ProviderLabel.BACKEND_STUB.value: {
        "display_label": "Backend stub provider",
        "real_chatbot_execution": False,
        "implemented": True,
        "supports_network": False,
    },
    ProviderLabel.LOCAL_CPU.value: {
        "display_label": "Local CPU provider",
        "real_chatbot_execution": True,
        "implemented": True,
        "supports_network": True,
    },
    ProviderLabel.GPU_LOCAL.value: {
        "display_label": "GPU-local provider",
        "real_chatbot_execution": False,
        "implemented": True,
        "supports_network": True,
        "mode": "no_model_smoke_or_explicit_transformers_lifecycle",
    },
    ProviderLabel.AWS_BEDROCK.value: {
        "display_label": "AWS Bedrock provider",
        "real_chatbot_execution": True,
        "implemented": False,
        "supports_network": True,
    },
    ProviderLabel.EXTERNAL_API.value: {
        "display_label": "External API provider",
        "real_chatbot_execution": True,
        "implemented": False,
        "supports_network": True,
    },
}


@dataclass(frozen=True)
class StageExecutionRequest:
    stage_id: str
    stage_label: str
    provider: str
    document_id: str
    document_title: str = ""
    document_type: str = ""
    requested_provider: str = ""
    request_id: str = field(default_factory=lambda: f"stage-request-{uuid4().hex}")
    schema_version: str = STAGE_EXECUTION_REQUEST_SCHEMA
    contract_version: str = STAGE_CONTRACT_VERSION
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    required_stage_ids: tuple[str, ...] = ()
    accepted_upstream_stage_ids: tuple[str, ...] = ()
    correction_requested: bool = False
    source_character_count: int = 0
    prompt_character_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_stage_ids"] = list(self.required_stage_ids)
        data["accepted_upstream_stage_ids"] = list(self.accepted_upstream_stage_ids)
        return data


@dataclass(frozen=True)
class StageExecutionResult:
    status: str
    raw_output: str
    payload: dict[str, Any]
    provenance: dict[str, Any]
    error: str = ""
    provider: str = ""
    model: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    schema_version: str = STAGE_EXECUTION_RESULT_SCHEMA
    contract_version: str = STAGE_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderApiPayload:
    request: StageExecutionRequest
    inputs: dict[str, Any]
    prompt_package: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)
    schema_version: str = STAGE_PROVIDER_API_PAYLOAD_SCHEMA
    contract_version: str = STAGE_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.request.schema_version,
            "contract_version": self.contract_version,
            "payload_schema_version": self.schema_version,
            "request_id": self.request.request_id,
            "stage_id": self.request.stage_id,
            "stage_label": self.request.stage_label,
            "provider": self.request.provider,
            "requested_provider": self.request.requested_provider,
            "document_id": self.request.document_id,
            "document_title": self.request.document_title,
            "document_type": self.request.document_type,
            "required_stage_ids": list(self.request.required_stage_ids),
            "accepted_upstream_stage_ids": list(self.request.accepted_upstream_stage_ids),
            "correction_requested": self.request.correction_requested,
            "inputs": self.inputs,
            "prompt_package": self.prompt_package,
            "options": self.options,
        }

    def as_redacted_dict(self) -> dict[str, Any]:
        payload = self.as_dict()
        prompt_package = dict(payload.get("prompt_package") or {})
        system_prompt = str(prompt_package.pop("system_prompt", "") or "")
        user_prompt = str(prompt_package.pop("user_prompt", "") or "")
        tokenization_diagnostics = prompt_package.pop("tokenization_diagnostics", None)
        if isinstance(tokenization_diagnostics, dict):
            prompt_package["tokenization_diagnostics"] = {
                "contract_version": str(
                    tokenization_diagnostics.get("contract_version") or ""
                ),
                "variants": [
                    str((tokenization_diagnostics.get(name) or {}).get("label") or name)
                    for name in ("baseline", "current")
                    if isinstance(tokenization_diagnostics.get(name), dict)
                ],
                "full_prompt_text_redacted": True,
                "out_of_band_provenance_redacted": True,
            }
        candidate_package = prompt_package.get("entity_candidate_package")
        if isinstance(candidate_package, dict):
            prompt_package["entity_candidate_package"] = {
                key: value
                for key, value in candidate_package.items()
                if key
                not in {
                    "candidate_provenance",
                    "excluded_candidates",
                    "query_coverage",
                    "document_scope",
                }
            }
            prompt_package["entity_candidate_package"]["audit_details_redacted"] = True
        prompt_package.update(
            {
                "system_prompt_present": bool(system_prompt),
                "user_prompt_present": bool(user_prompt),
                "system_prompt_character_count": len(system_prompt),
                "user_prompt_character_count": len(user_prompt),
            }
        )
        inputs = dict(payload.get("inputs") or {})
        if "document_body" in inputs:
            inputs["document_body_character_count"] = len(str(inputs.pop("document_body") or ""))
        if "document_header" in inputs:
            inputs["document_header_character_count"] = len(str(inputs.pop("document_header") or ""))
        upstream_outputs = inputs.get("upstream_outputs")
        if isinstance(upstream_outputs, dict):
            inputs["upstream_outputs"] = {
                stage_id: {
                    "status": str(output.get("status") or ""),
                    "raw_output_character_count": len(str(output.get("raw_output") or "")),
                    "payload_keys": sorted((output.get("payload") or {}).keys())
                    if isinstance(output.get("payload"), dict)
                    else [],
                    "provenance_keys": sorted((output.get("provenance") or {}).keys())
                    if isinstance(output.get("provenance"), dict)
                    else [],
                }
                for stage_id, output in upstream_outputs.items()
                if isinstance(output, dict)
            }
        payload["inputs"] = inputs
        payload["prompt_package"] = prompt_package
        payload["redacted"] = True
        return payload


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_provider_label(provider: str) -> str:
    raw = str(provider or "").strip().lower()
    if not raw:
        return ProviderLabel.BACKEND_STUB.value
    normalized = PROVIDER_ALIASES.get(raw)
    if not normalized:
        raise ValueError(f"Unsupported provider label: {provider}")
    return normalized


def provider_capability(provider: str) -> dict[str, Any]:
    return dict(PROVIDER_CAPABILITIES[normalize_provider_label(provider)])


def build_provider_provenance(
    *,
    request: StageExecutionRequest,
    provider: str,
    model: str,
    status: str,
    started_at: str,
    finished_at: str | None = None,
    real_chatbot_execution: bool = False,
    validation: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_label = normalize_provider_label(provider)
    payload = {
        "source": "chatbot_stage_runner",
        "contract_version": STAGE_CONTRACT_VERSION,
        "request_schema": STAGE_EXECUTION_REQUEST_SCHEMA,
        "result_schema": STAGE_EXECUTION_RESULT_SCHEMA,
        "request": request.as_dict(),
        "provider": provider_label,
        "provider_label": provider_label,
        "requested_provider": request.requested_provider or provider_label,
        "provider_capability": provider_capability(provider_label),
        "model": model,
        "stage_id": request.stage_id,
        "stage_label": request.stage_label,
        "execution_status": status,
        "started_at": started_at,
        "finished_at": finished_at or now_iso(),
        "real_chatbot_execution": real_chatbot_execution,
        "validation": validation or {},
        "warnings": warnings or [],
        "errors": errors or [],
    }
    if extra:
        payload.update(extra)
    return payload
