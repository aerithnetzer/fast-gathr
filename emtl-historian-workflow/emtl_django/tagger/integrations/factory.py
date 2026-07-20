from __future__ import annotations

import os
from pathlib import Path

from .local_artifacts import LocalArtifactStore
from .stubs import (
    UnconfiguredAwsChatbotProvider,
    UnconfiguredPostgresRepository,
    UnconfiguredS3ArtifactStore,
)


def chatbot_provider_from_env():
    name = os.getenv("EMTL_CHATBOT_PROVIDER", "unconfigured").strip().lower()
    if name in {"", "unconfigured", "aws_bedrock"}:
        return UnconfiguredAwsChatbotProvider()
    raise ValueError(f"Unknown EMTL_CHATBOT_PROVIDER: {name}")


def artifact_store_from_env():
    name = os.getenv("EMTL_ARTIFACT_STORE", "local").strip().lower()
    if name == "local":
        return LocalArtifactStore(Path(os.getenv("EMTL_LOCAL_ARTIFACT_ROOT", "outputs/artifacts")))
    if name in {"s3", "unconfigured"}:
        return UnconfiguredS3ArtifactStore()
    raise ValueError(f"Unknown EMTL_ARTIFACT_STORE: {name}")


def workflow_repository_from_env():
    name = os.getenv("EMTL_WORKFLOW_REPOSITORY", "unconfigured").strip().lower()
    if name in {"postgresql", "unconfigured", ""}:
        return UnconfiguredPostgresRepository()
    raise ValueError(f"Unknown EMTL_WORKFLOW_REPOSITORY: {name}")
