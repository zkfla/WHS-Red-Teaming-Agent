"""Backward-compatible names for the public immutable artifact port."""

from redteam.core.ports.artifacts import ArtifactStore as ArtifactBackend
from redteam.core.ports.artifacts import ArtifactWriteResult

__all__ = ["ArtifactBackend", "ArtifactWriteResult"]
