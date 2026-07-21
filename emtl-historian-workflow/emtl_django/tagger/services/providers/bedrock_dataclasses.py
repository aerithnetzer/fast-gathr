"""Result dataclasses for the AWS Bedrock provider.

These mirror the shapes of the GpuLocal* dataclasses in ``gpu_local.py`` so the
Entity Registry runner and the generic stage runner can treat a Bedrock client
interchangeably with the local GPU client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import ProviderLabel


@dataclass(frozen=True)
class BedrockProviderResult:
    status: str
    raw_output: str = ""
    provider: str = ProviderLabel.AWS_BEDROCK.value
    model: str = ""
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    real_chatbot_execution: bool = False


@dataclass(frozen=True)
class BedrockProviderHealthResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class BedrockTokenizationPreflightResult:
    status: str
    provider: str = ProviderLabel.AWS_BEDROCK.value
    model: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
