from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


INTEGRATION_CONTRACT_VERSION = "emtl-handoff-contract-v1"
JOB_STATES = {"queued", "running", "completed", "failed", "cancelled"}


class IntegrationNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class JobReceipt:
    job_id: str
    request_id: str
    state: str
    submitted_at: str
    provider: str
    status_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: str
    updated_at: str
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactReference:
    uri: str
    bucket: str
    key: str
    version_id: str
    sha256: str
    content_type: str
    size_bytes: int
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitReceipt:
    commit_id: str
    export_id: str
    state: str
    committed_at: str
    record_counts: dict[str, int] = field(default_factory=dict)


class AsyncChatbotProvider(Protocol):
    provider_name: str

    def submit(self, request: dict[str, Any]) -> JobReceipt: ...

    def status(self, job_id: str) -> JobStatus: ...

    def result(self, job_id: str) -> dict[str, Any]:
        """Return an emtl-stage-execution-result-v1 compatible object."""


class ArtifactStore(Protocol):
    def put(
        self, *, key: str, content: bytes, content_type: str,
        sha256: str, metadata: dict[str, str] | None = None,
    ) -> ArtifactReference: ...

    def get(self, reference: ArtifactReference) -> bytes: ...

    def presign_get(self, reference: ArtifactReference, *, expires_seconds: int) -> str: ...


class WorkflowRepository(Protocol):
    def health(self) -> dict[str, Any]: ...

    def commit_export(
        self, package: dict[str, Any], *, idempotency_key: str
    ) -> CommitReceipt: ...

    def get_export(self, export_id: str) -> dict[str, Any]: ...
