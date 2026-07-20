from __future__ import annotations

import os
from typing import Any, Protocol

from ..contracts import ProviderLabel, normalize_provider_label
from .gpu_local import GpuLocalProviderClient


class StageGenerationClient(Protocol):
    provider_name: str

    def generate(self, provider_payload: dict[str, Any]) -> Any: ...


class ProviderIntegrationRequired(RuntimeError):
    pass


def stage_generation_client(
    requested_provider: str | None = None,
) -> StageGenerationClient:
    configured = str(
        requested_provider or os.getenv("EMTL_STAGE_PROVIDER") or "gpu_local"
    ).strip()
    if configured.lower() in {"", "unconfigured"}:
        raise ProviderIntegrationRequired(
            "No live stage provider is configured. Set EMTL_STAGE_PROVIDER after "
            "installing the corresponding adapter."
        )
    provider = normalize_provider_label(configured)
    if provider == ProviderLabel.GPU_LOCAL.value:
        return GpuLocalProviderClient()
    if provider in {
        ProviderLabel.AWS_BEDROCK.value,
        ProviderLabel.EXTERNAL_API.value,
    }:
        raise ProviderIntegrationRequired(
            f"{provider} is selected but no StageGenerationClient adapter is installed. "
            "Implement it in tagger/services/providers/factory.py and return it from "
            "stage_generation_client()."
        )
    raise ProviderIntegrationRequired(
        f"Provider {provider!r} is not a live stage generation provider."
    )


def local_controlled_generation_client() -> GpuLocalProviderClient:
    client = stage_generation_client()
    if not isinstance(client, GpuLocalProviderClient):
        raise ProviderIntegrationRequired(
            "The current controlled Entity runner requires local health and tokenization "
            "capabilities. Route the AWS Entity implementation through the same factory "
            "after implementing equivalent server-side preflight policy."
        )
    return client
