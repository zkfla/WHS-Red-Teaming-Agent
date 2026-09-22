"""ID / Enum Test: typed IDs stay distinguishable, enums carry the right values."""

from __future__ import annotations

from redteam.core.enums import (
    AgentPhase,
    ApprovalStatus,
    EvidenceSourceType,
    ExecutionStatus,
    GraphEdgeType,
    GraphNodeType,
    PolicyVerdict,
    RiskLevel,
    RunStatus,
    ToolHealthStatus,
    Verdict,
)
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    AssetId,
    EngagementId,
    EventId,
    EvidenceId,
    FindingId,
    GraphEdgeId,
    GraphNodeId,
    HypothesisId,
    InvocationId,
    PrincipalId,
    RunId,
    ToolId,
    new_id,
)


def test_new_id_is_prefixed_and_unique() -> None:
    a = new_id("run")
    b = new_id("run")
    assert a.startswith("run_")
    assert a != b


def test_typed_ids_are_plain_strings_at_runtime() -> None:
    # NewType is a static-only distinction — every one of these must
    # still behave like `str` at runtime (equality, hashing, JSON).
    for make, prefix in [
        (RunId, "run"),
        (EngagementId, "eng"),
        (AgentId, "agent"),
        (ActionId, "action"),
        (InvocationId, "inv"),
        (ToolId, "tool"),
        (EvidenceId, "ev"),
        (HypothesisId, "hyp"),
        (FindingId, "finding"),
        (EventId, "evt"),
        (ApprovalId, "appr"),
        (GraphNodeId, "node"),
        (GraphEdgeId, "edge"),
        (PrincipalId, "principal"),
        (AssetId, "asset"),
    ]:
        value = make(new_id(prefix))
        assert isinstance(value, str)
        assert value == str(value)


def test_agent_phase_has_bootstrap_and_terminal_states() -> None:
    values = {p.value for p in AgentPhase}
    assert {"bootstrap", "finished", "failed"} <= values


def test_policy_verdict_values() -> None:
    assert {v.value for v in PolicyVerdict} == {"allow", "deny", "require_approval"}


def test_verdict_values() -> None:
    assert {v.value for v in Verdict} == {"pass", "violation", "anomaly", "unknown"}


def test_evidence_source_type_values() -> None:
    assert {v.value for v in EvidenceSourceType} == {"tool", "agent", "system", "operator"}


def test_run_status_and_execution_status_and_health_and_risk_and_approval_exist() -> None:
    assert {v.value for v in RunStatus} >= {"pending", "running", "finished", "failed"}
    assert {v.value for v in ExecutionStatus} >= {"pending", "running", "succeeded", "failed"}
    assert {v.value for v in ToolHealthStatus} == {"healthy", "unhealthy", "unknown"}
    assert {v.value for v in RiskLevel} == {"low", "medium", "high", "critical"}
    assert {v.value for v in ApprovalStatus} == {"pending", "approved", "rejected", "expired"}


def test_graph_node_and_edge_type_values() -> None:
    assert "host" in {v.value for v in GraphNodeType}
    assert "hosts" in {v.value for v in GraphEdgeType}
