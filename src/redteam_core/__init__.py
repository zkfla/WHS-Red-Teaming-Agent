"""Domain-neutral red-team core.

Concrete domains register typed contracts through the extension catalog; the core owns only
cross-domain mechanisms, invariants, and canonical state transitions.
"""

from redteam_core.models.base import SchemaModel, TriState, stable_id
from redteam_core.models.provenance import (
    Evidence,
    EvidenceArtifactRef,
    EvidenceStoragePolicy,
    Provenance,
    SensitivityClass,
    TrustClass,
    TypedPayloadEnvelope,
)
from redteam_core.models.security import (
    CandidateDisposition,
    ClaimContract,
    ClaimStatus,
    Finding,
    MissingReason,
    MissingReasonKind,
    Relation,
    SecurityCandidate,
    ValidationOutcome,
    ValidationVerdict,
)

__all__ = [
    "CandidateDisposition",
    "ClaimContract",
    "ClaimStatus",
    "Evidence",
    "EvidenceArtifactRef",
    "EvidenceStoragePolicy",
    "Finding",
    "MissingReason",
    "MissingReasonKind",
    "Provenance",
    "Relation",
    "SchemaModel",
    "SecurityCandidate",
    "SensitivityClass",
    "TriState",
    "TrustClass",
    "TypedPayloadEnvelope",
    "ValidationOutcome",
    "ValidationVerdict",
    "stable_id",
]
