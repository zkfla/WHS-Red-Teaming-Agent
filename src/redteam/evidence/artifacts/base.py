"""Backward-compatible imports for the existing immutable blob port."""

from redteam.core.ports.artifacts import ArtifactStore as ArtifactBackend
from redteam.core.ports.artifacts import ArtifactWriteResult

__all__ = ["ArtifactBackend", "ArtifactWriteResult"]
