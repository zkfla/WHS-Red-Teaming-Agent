"""Scope Deny Test: out-of-scope/denied targets never get ALLOW."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.scope import check_scope
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import HostTarget
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _proposal(host: str) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def test_allowed_host_passes() -> None:
    scope = EngagementScope(allowed_hosts=["good.local"])
    ok, reasons = check_scope(_proposal("good.local"), scope)
    assert ok is True
    assert reasons == []


def test_host_outside_allow_list_is_denied() -> None:
    scope = EngagementScope(allowed_hosts=["good.local"])
    ok, reasons = check_scope(_proposal("unknown.local"), scope)
    assert ok is False
    assert reasons


def test_explicitly_denied_host_is_denied_even_if_allowed() -> None:
    scope = EngagementScope(allowed_hosts=["bad.local"], denied_hosts=["bad.local"])
    ok, reasons = check_scope(_proposal("bad.local"), scope)
    assert ok is False
    assert reasons


def test_empty_scope_defaults_to_deny() -> None:
    scope = EngagementScope()
    ok, reasons = check_scope(_proposal("anything"), scope)
    assert ok is False
    assert reasons
