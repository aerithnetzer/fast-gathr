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
    if provider == ProviderLabel.AWS_BEDROCK.value:
        from .aws_bedrock import BedrockProviderClient

        return BedrockProviderClient()
    if provider == ProviderLabel.EXTERNAL_API.value:
        raise ProviderIntegrationRequired(
            f"{provider} is selected but no StageGenerationClient adapter is installed. "
            "Implement it in tagger/services/providers/factory.py and return it from "
            "stage_generation_client()."
        )
    raise ProviderIntegrationRequired(
        f"Provider {provider!r} is not a live stage generation provider."
    )


def local_controlled_generation_client():
    """Return the client for the controlled Entity Registry runner.

    Accepts the local GPU client or the Bedrock client. Both preserve the
    Entity stage's prompt-completeness and real-execution-evidence guarantees;
    the Bedrock path substitutes managed-service checks for the GPU-hardware
    preflight (see entity_generation.py).
    """
    client = stage_generation_client()
    from .aws_bedrock import BedrockProviderClient

    if isinstance(client, (GpuLocalProviderClient, BedrockProviderClient)):
        return client
    raise ProviderIntegrationRequired(
        "The controlled Entity runner requires a client that preserves prompt "
        "completeness and real-execution evidence. Route the adapter through "
        "stage_generation_client() first."
    )
