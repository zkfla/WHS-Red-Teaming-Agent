from __future__ import annotations

from enum import StrEnum

from redteam_core.models.provenance import (
    Evidence,
    EvidenceArtifactRef,
    EvidenceStoragePolicy,
)


class RawArtifactPurpose(StrEnum):
    RETAIN = "RETAIN"
    EXPORT = "EXPORT"
    LLM_CONTEXT = "LLM_CONTEXT"


class EvidenceStorageViolation(RuntimeError):
    pass


def authorize_raw_artifact_use(
    evidence: Evidence,
    *,
    policy: EvidenceStoragePolicy,
    purpose: RawArtifactPurpose,
) -> EvidenceArtifactRef:
    """Fail-closed policy boundary for retaining or exposing a raw evidence artifact.

    The actual storage backend remains adapter-owned. Trusted adapters must cross this boundary
    before retaining, exporting, or placing raw artifact bytes in model-visible context.
    """

    canonical = Evidence.model_validate(evidence.model_dump(mode="python"))
    artifact = canonical.raw_artifact_ref
    if artifact is None:
        raise EvidenceStorageViolation("evidence has no raw artifact reference")
    if not policy.allows_raw(
        sensitivity=canonical.sensitivity_class,
        size_bytes=artifact.size_bytes,
    ):
        raise EvidenceStorageViolation(
            "raw artifact retention is forbidden by sensitivity or size policy"
        )
    if purpose is RawArtifactPurpose.EXPORT and not policy.export_raw_allowed:
        raise EvidenceStorageViolation("raw artifact export is forbidden by policy")
    if purpose is RawArtifactPurpose.LLM_CONTEXT and not policy.llm_raw_allowed:
        raise EvidenceStorageViolation("raw artifact LLM exposure is forbidden by policy")
    return artifact
