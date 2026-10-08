"""Immutable blobs shared by evidence storage and runtime-local artifact tools."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    artifact_ref: str
    digest: str
    content_type: str
    size_bytes: int


class ArtifactStore(Protocol):
    async def put(self, data: bytes, content_type: str) -> ArtifactWriteResult: ...

    async def get(self, artifact_ref: str) -> bytes: ...

    async def verify(self, artifact_ref: str, expected_digest: str) -> bool: ...
