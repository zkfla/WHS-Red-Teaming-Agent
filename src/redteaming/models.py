from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from urllib.parse import urlparse
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix: str, *parts: str) -> str:
    value = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{sha256(value).hexdigest()[:24]}"


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class FindingStatus(str, Enum):
    VALIDATED = "validated"
    RETEST_REQUIRED = "retest_required"
    FIXED = "fixed"


@dataclass(frozen=True)
class Scope:
    """Runtime-provided authorization boundary; never encoded in a detector."""

    allowed_hosts: tuple[str, ...]
    allowed_schemes: tuple[str, ...] = ("http", "https")
    max_requests_per_minute: int = 60
    max_concurrency: int = 8
    excluded_hosts: tuple[str, ...] = ()

    def allows(self, target: str) -> bool:
        """Return whether target matches one explicit host/scheme/port entry."""
        parsed = urlparse(target if "://" in target else f"//{target}")
        host = (parsed.hostname or target).lower().rstrip(".")
        excluded = {item.lower().rstrip(".") for item in self.excluded_hosts}
        if host in excluded:
            return False
        if parsed.scheme and parsed.scheme.lower() not in {
            scheme.lower() for scheme in self.allowed_schemes
        }:
            return False

        target_scheme = parsed.scheme.lower()
        target_port = parsed.port
        if target_port is None and target_scheme in {"http", "https"}:
            target_port = 443 if target_scheme == "https" else 80

        # Match declarations independently. A scheme/port for one host must
        # never authorize a different host, and an explicit HTTPS :443 entry
        # must also match the URL's implicit default port.
        for item in self.allowed_hosts:
            item_parsed = urlparse(item if "://" in item else f"//{item}")
            item_host = (item_parsed.hostname or item).lower().rstrip(".")
            if item_host != host:
                continue
            if item_parsed.scheme and parsed.scheme:
                if item_parsed.scheme.lower() != target_scheme:
                    continue
            if item_parsed.port is not None and item_parsed.port != target_port:
                continue
            return True
        return False


@dataclass(frozen=True)
class TaskSpec:
    name: str
    scope: Scope
    detector_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Asset:
    canonical_value: str
    kind: str
    id: str = field(default_factory=lambda: new_id("asset"))
    parent_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Intent:
    objective: str
    target_asset_ids: tuple[str, ...]
    kind: str
    id: str = field(default_factory=lambda: new_id("intent"))
    depends_on: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    kind: str
    summary: str
    id: str = field(default_factory=lambda: new_id("evidence"))
    raw_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VulnerabilityCandidate:
    category: str
    title: str
    asset_ids: tuple[str, ...]
    hypothesis: str
    test_plan: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: new_id("candidate"))
    confidence: float = 0.0
    status: CandidateStatus = CandidateStatus.CANDIDATE
    # A detector may provide a stable cause shared by multiple endpoints.
    # Empty values are normalized by the runtime from category/title for
    # backward-compatible custom detectors.
    root_cause: str = ""


@dataclass(frozen=True)
class VerificationResult:
    candidate_id: str
    status: CandidateStatus
    evidence: tuple[Evidence, ...]
    reproduction_steps: tuple[str, ...]
    observed_impact: str
    confidence: float


@dataclass(frozen=True)
class Finding:
    title: str
    category: str
    severity: str
    asset_ids: tuple[str, ...]
    reproduction_steps: tuple[str, ...]
    impact: str
    evidence_ids: tuple[str, ...]
    remediation: str
    id: str = field(default_factory=lambda: new_id("finding"))
    status: FindingStatus = FindingStatus.VALIDATED
    # Links the published Finding back to the verified hypothesis.  The
    # default keeps snapshots created before this field backward-compatible.
    candidate_id: str = ""
    # Stable grouping key for the underlying cause, independent of endpoint.
    root_cause: str = ""
