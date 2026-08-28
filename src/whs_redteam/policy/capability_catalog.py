"""Code-owned minimum safety properties for every agent capability."""

from __future__ import annotations

from dataclasses import dataclass

from whs_redteam.models import Capability, RiskLevel


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    minimum_risk: RiskLevel
    allowed_methods: frozenset[str]
    approval_required: bool = False
    cleanup_required: bool = False


SAFE_READ = frozenset({"GET", "HEAD", "OPTIONS"})
SAFE_WITH_WRITE = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"})

CAPABILITY_CATALOG: dict[Capability, CapabilitySpec] = {
    Capability.DISCOVERY: CapabilitySpec(RiskLevel.PASSIVE, SAFE_READ),
    Capability.AUTH_SESSION: CapabilitySpec(RiskLevel.SAFE_ACTIVE, SAFE_READ),
    Capability.ROLE_COMPARE: CapabilitySpec(RiskLevel.SAFE_ACTIVE, SAFE_READ),
    Capability.API_SCHEMA: CapabilitySpec(RiskLevel.PASSIVE, SAFE_READ),
    Capability.RESOURCE_MODEL: CapabilitySpec(RiskLevel.PASSIVE, SAFE_READ),
    Capability.INPUT_MUTATION: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.SQLI_SAFE: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.XSS_SAFE: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.IDOR_SAFE: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.CSRF_SAFE: CapabilitySpec(
        RiskLevel.STATE_CHANGING,
        frozenset({"POST", "PUT", "PATCH"}),
        approval_required=True,
        cleanup_required=True,
    ),
    Capability.SSRF_CANARY: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.FILE_UPLOAD_BENIGN: CapabilitySpec(
        RiskLevel.STATE_CHANGING,
        frozenset({"POST", "PUT"}),
        approval_required=True,
        cleanup_required=True,
    ),
    Capability.PATH_TRAVERSAL_CANARY: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_READ, approval_required=True
    ),
    Capability.BUSINESS_LOGIC: CapabilitySpec(
        RiskLevel.STATE_CHANGING,
        SAFE_WITH_WRITE,
        approval_required=True,
        cleanup_required=True,
    ),
    Capability.POC_SAFE: CapabilitySpec(
        RiskLevel.SAFE_ACTIVE, SAFE_WITH_WRITE, approval_required=True
    ),
    Capability.LLM_PLANNING: CapabilitySpec(RiskLevel.PASSIVE, frozenset({"GET"})),
    Capability.FINDING_VALIDATION: CapabilitySpec(RiskLevel.SAFE_ACTIVE, SAFE_READ),
}

if set(CAPABILITY_CATALOG) != set(Capability):
    raise RuntimeError("every capability must have exactly one safety specification")
