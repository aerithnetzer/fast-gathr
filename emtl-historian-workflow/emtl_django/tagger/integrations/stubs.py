from __future__ import annotations

from typing import Any

from .contracts import IntegrationNotConfigured


class UnconfiguredAwsChatbotProvider:
    provider_name = "aws_bedrock"

    def _fail(self):
        raise IntegrationNotConfigured(
            "AWS chatbot provider is not configured. Implement AsyncChatbotProvider and select it with EMTL_CHATBOT_PROVIDER."
        )

    def submit(self, request: dict[str, Any]): self._fail()
    def status(self, job_id: str): self._fail()
    def result(self, job_id: str): self._fail()


class UnconfiguredS3ArtifactStore:
    def _fail(self):
        raise IntegrationNotConfigured(
            "S3 artifact storage is not configured. Implement ArtifactStore and set EMTL_ARTIFACT_STORE=s3."
        )

    def put(self, **kwargs): self._fail()
    def get(self, reference): self._fail()
    def presign_get(self, reference, *, expires_seconds: int): self._fail()


class UnconfiguredPostgresRepository:
    def _fail(self):
        raise IntegrationNotConfigured(
            "PostgreSQL repository is not configured. Apply the handoff DDL and implement WorkflowRepository."
        )

    def health(self) -> dict[str, Any]: self._fail()
    def commit_export(self, package: dict[str, Any], *, idempotency_key: str): self._fail()
    def get_export(self, export_id: str): self._fail()
