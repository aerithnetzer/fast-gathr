from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import ArtifactReference


class LocalArtifactStore:
    """Development adapter matching the S3 boundary without pretending to be S3."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, *, key: str, content: bytes, content_type: str, sha256: str,
            metadata: dict[str, str] | None = None) -> ArtifactReference:
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            raise ValueError("Artifact SHA-256 does not match supplied checksum.")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("Artifact key escapes the configured root.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return ArtifactReference(
            uri=path.as_uri(), bucket="", key=key, version_id="local-v1",
            sha256=actual, content_type=content_type, size_bytes=len(content),
            metadata=metadata or {},
        )

    def get(self, reference: ArtifactReference) -> bytes:
        path = (self.root / reference.key).resolve()
        if self.root not in path.parents:
            raise ValueError("Artifact key escapes the configured root.")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("Stored artifact checksum mismatch.")
        return content

    def presign_get(self, reference: ArtifactReference, *, expires_seconds: int) -> str:
        raise NotImplementedError("Local artifacts do not issue presigned URLs.")
