"""Gated registry pin canonicalization, manifest shape, and strategy refusal.

Owned by Capability (TASK-005). No docker, no network: every case runs
against the provider contracts only.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.capability.gated_manifest import (
    OPERATION_PURPOSE,
    SHELL_INSTALL_RUN,
    SHELL_TARGET_JOB_START,
    SHELL_TARGET_RUN,
    TARGET_TYPES,
    build_gated_descriptors,
    gated_body_operation,
    gated_operation_for,
    gated_purpose_for_operation,
    validate_gated_descriptors,
)
from redteam.capability.gated_network_policy import PinnedRegistry
from redteam.capability.gated_strategy import GatedExecutionPolicy
from redteam.core.contracts.network import GrantRefused, NetworkPurpose
from redteam.core.contracts.target import DomainTarget, TargetRef
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.enums import RiskLevel
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)


def test_ipv6_alias_across_sections_rejected() -> None:
    """Long-form v6 in mirrors must not bypass the disjoint check via text."""
    with pytest.raises(ValueError):
        PinnedRegistry(targets={"a": ("::1",)}, mirrors={"m": ("0:0:0:0:0:0:0:1",)})


def test_alias_within_one_section_rejected() -> None:
    with pytest.raises(ValueError):
        PinnedRegistry(targets={"a": ("::1",), "b": ("0:0:0:0:0:0:0:1",)}, mirrors={})


def test_noncanonical_ipv4_rejected() -> None:
    with pytest.raises(ValueError):
        PinnedRegistry(targets={"a": ("10.0.0.01",)}, mirrors={})


def test_canonical_pins_accepted() -> None:
    registry = PinnedRegistry(
        targets={"a": ("10.0.0.1", "::1")}, mirrors={"m": ("10.0.0.2",)}
    )
    assert registry.target_ips() == ("10.0.0.1", "::1")
    assert registry.mirror_ips() == ("10.0.0.2",)


def test_same_text_disjoint_still_rejected() -> None:
    with pytest.raises(ValueError):
        PinnedRegistry(targets={"a": ("10.0.0.9",)}, mirrors={"m": ("10.0.0.9",)})


def test_manifest_shape() -> None:
    assert TARGET_TYPES == ["host", "domain", "url", "endpoint", "service"]
    assert OPERATION_PURPOSE == {
        SHELL_TARGET_RUN: NetworkPurpose.TARGET,
        SHELL_TARGET_JOB_START: NetworkPurpose.TARGET,
        SHELL_INSTALL_RUN: NetworkPurpose.INSTALL,
    }
    descriptors = build_gated_descriptors(None)
    validate_gated_descriptors(descriptors, None)
    assert [d.tool_name for d in descriptors] == [
        SHELL_TARGET_RUN,
        SHELL_TARGET_JOB_START,
        SHELL_INSTALL_RUN,
    ]
    for descriptor in descriptors:
        assert descriptor.requires_target is True
        assert list(descriptor.target_types) == TARGET_TYPES
        assert descriptor.risk_level == RiskLevel.MEDIUM
    bodies = {d.tool_name: gated_body_operation(d) for d in descriptors}
    assert bodies[SHELL_TARGET_RUN] == "shell_run"
    assert bodies[SHELL_TARGET_JOB_START] == "shell_job_start"
    assert bodies[SHELL_INSTALL_RUN] == "shell_run"
    assert [d.operations for d in descriptors] == [
        [SHELL_TARGET_RUN, "shell_run"],
        [SHELL_TARGET_JOB_START, "shell_job_start"],
        [SHELL_INSTALL_RUN, "shell_run"],
    ]
    assert gated_operation_for(SHELL_TARGET_RUN) == SHELL_TARGET_RUN
    assert gated_operation_for("shell_run") is None
    assert gated_purpose_for_operation(SHELL_INSTALL_RUN) == NetworkPurpose.INSTALL
    assert gated_purpose_for_operation("shell_run") is None


def _invocation(*, tool_name: str, approved: TargetRef | None) -> ToolInvocation:
    now = datetime.now(timezone.utc)
    context = ExecutionContext(
        engagement_id=EngagementId("eng_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=approved,
    )
    return ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis." + tool_name),
        arguments={"language": "bash", "script": "true", "target_ref": "a", "port": 8080},
        execution_context=context,
        created_at=now,
    )


def test_strategy_refuses_without_approved_target() -> None:
    policy = GatedExecutionPolicy(None)
    result = policy.resolve(_invocation(tool_name=SHELL_TARGET_RUN, approved=None))
    assert isinstance(result, GrantRefused)
    assert result.code == "gated_target_missing"


def test_strategy_refuses_unknown_operation() -> None:
    policy = GatedExecutionPolicy(None)
    approved = DomainTarget(domain="example.com")
    result = policy.resolve(_invocation(tool_name="shell_run", approved=approved))
    assert isinstance(result, GrantRefused)
    assert result.code == "gated_unknown_operation"
