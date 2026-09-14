"""Shared enumerations for Core Contracts.

Uses the ``str, Enum`` mixin (not ``enum.StrEnum``) so the codebase stays
importable on Python 3.10, which is what's available in local dev right
now even though the project targets the latest stable Python line. Swap
to ``enum.StrEnum`` once the team is fully on 3.11+ — the string values
below are identical either way.
"""

from __future__ import annotations

from enum import Enum


class AgentPhase(str, Enum):
    """LangGraph node phase for a single run — see ``orchestration.state.RuntimeState``."""

    BOOTSTRAP = "bootstrap"
    OBSERVE = "observe"
    PLAN = "plan"
    POLICY = "policy"
    EXECUTE = "execute"
    VERIFY = "verify"
    UPDATE = "update"
    REPLAN = "replan"
    FINISHED = "finished"
    FAILED = "failed"


class RunStatus(str, Enum):
    """Coarse lifecycle status of an engagement Run.

    Distinct from ``AgentPhase``: phase is "which LangGraph node is
    active," status is "is the run still going." Not currently a field
    on ``RuntimeState`` (instruction.md section 17 keeps that to IDs +
    phase) — available for a future Run-tracking contract or for
    reporting/audit consumers.
    """

    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolicyVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class Verdict(str, Enum):
    """Outcome of verifying an observation/hypothesis against expectations.

    **Not the same axis as** ``ToolResult.success``/``ExecutionStatus``
    (did the tool call execute without error) — the two are independent
    and both are needed to interpret a result:

    - ``success=True``  + ``PASS``      -> tool ran fine, hypothesis not
      confirmed / no violation found
    - ``success=True``  + ``VIOLATION`` -> tool ran fine, and it
      confirmed a security violation
    - ``success=False`` + ``UNKNOWN``   -> tool execution failed, so no
      verdict could be reached
    - ``success=False`` + ``PASS``/``VIOLATION`` is possible too (e.g. a
      prior observation already gave a verdict; this action's failure
      doesn't retract it) — never *infer* ``Verdict`` from ``success``
      alone.

    Never use this enum as a stand-in for "did the action execute
    successfully" — that's what ``ToolResult.success`` /
    ``ExecutionStatus`` are for. There is currently no real verification
    logic computing this (see ``orchestration.nodes.verify`` TODO) — no
    new contract (e.g. a ``VerificationResult``) is planned for this
    stage; if one turns out to be needed it's Agent Team's call when
    real hypothesis verification lands.
    """

    PASS = "pass"
    VIOLATION = "violation"
    ANOMALY = "anomaly"
    UNKNOWN = "unknown"


class EvidenceSourceType(str, Enum):
    TOOL = "tool"
    AGENT = "agent"
    SYSTEM = "system"
    OPERATOR = "operator"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolHealthStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ExecutionStatus(str, Enum):
    """Optional lifecycle status for a ``ToolInvocation`` in flight.

    Not wired into ``ToolResult`` (which only needs the terminal
    ``success`` flag) — available for a Capability Team gateway that
    wants to publish intermediate PENDING/RUNNING state.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class GraphNodeType(str, Enum):
    HOST = "host"
    SERVICE = "service"
    ENDPOINT = "endpoint"
    PRINCIPAL = "principal"
    CREDENTIAL = "credential"
    ASSET = "asset"
    RESOURCE = "resource"
    HYPOTHESIS = "hypothesis"
    FINDING = "finding"


class GraphEdgeType(str, Enum):
    HOSTS = "hosts"
    EXPOSES = "exposes"
    AUTHENTICATES_TO = "authenticates_to"
    CAN_ACCESS = "can_access"
    OWNS = "owns"
    OBSERVED = "observed"
    RELATED_TO = "related_to"
    TARGETS = "targets"
    SUPPORTED_BY = "supported_by"
    DERIVED_FROM = "derived_from"
