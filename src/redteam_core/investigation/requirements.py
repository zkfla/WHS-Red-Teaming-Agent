"""Compatibility exports for the canonical requirement data contracts."""

from redteam_core.models.requirements import (
    EvidenceFreshness,
    EvidenceRequirement,
    Observation,
    ObservationValidity,
    ResolutionFailureKind,
    ResolutionFailureRecord,
)

__all__ = [
    "EvidenceFreshness",
    "EvidenceRequirement",
    "Observation",
    "ObservationValidity",
    "ResolutionFailureKind",
    "ResolutionFailureRecord",
]
