"""Track B internal raw Artifact storage.

This package is not a shared Core port.  Callers outside the Control-owned
Evidence package must wait for the common contract currently under PM review.
"""

from redteam.evidence.artifacts.base import ArtifactBackend, ArtifactWriteResult
from redteam.evidence.artifacts.errors import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReferenceError,
    ArtifactTooLargeError,
    ArtifactWriteError,
)
from redteam.evidence.artifacts.filesystem import FilesystemArtifactBackend

__all__ = [
    "ArtifactBackend",
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactReferenceError",
    "ArtifactTooLargeError",
    "ArtifactWriteError",
    "ArtifactWriteResult",
    "FilesystemArtifactBackend",
]
