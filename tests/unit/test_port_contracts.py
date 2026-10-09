"""Port Contract Test: Fake implementations satisfy the Core Port Protocols.

Typed assignments below are checked statically by mypy (a Fake that
doesn't match its Protocol fails ``mypy src/redteam tests`` in CI); the
runtime calls give a lightweight smoke test on top of that.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, AgentId, EngagementId, InvocationId, RunId, ToolId
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_gateway import ToolGateway
from tests.fakes import (
    DummyToolGateway,
    FakeAttackGraphStore,
    FakeControlPlane,
    FakeEventBus,
    FakeEvidenceStore,
    FakeLLMClient,
)


async def test_fake_control_plane_satisfies_protocol() -> None:
    control_plane: ControlPlane = FakeControlPlane()
    proposal = ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host="a.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )
    decision = await control_plane.evaluate(
        proposal, ControlContext(engagement_id=EngagementId("eng_1"))
    )
    assert decision.action_id == proposal.action_id


async def test_dummy_tool_gateway_satisfies_protocol() -> None:
    gateway: ToolGateway = DummyToolGateway()
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        tool_id=ToolId("recon.ping"),
        operation="service_discovery",
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_1"), run_id=RunId("run_1")
        ),
        created_at=datetime.now(timezone.utc),
    )
    result = await gateway.invoke(invocation)
    assert result.invocation_id == invocation.invocation_id


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), 0, -1])
def test_invalid_invocation_deadline_is_rejected_before_gateway(timeout: float) -> None:
    with pytest.raises(ValidationError, match="finite and > 0"):
        ToolInvocation(
            invocation_id=InvocationId("invalid-deadline"), action_id=ActionId("action_1"),
            run_id=RunId("run_1"), tool_id=ToolId("recon.ping"), timeout_seconds=timeout,
            execution_context=ExecutionContext(
                engagement_id=EngagementId("eng_1"), run_id=RunId("run_1"),
            ), created_at=datetime.now(timezone.utc),
        )


async def test_fake_evidence_store_satisfies_protocol() -> None:
    store: EvidenceStore = FakeEvidenceStore()
    assert await store.list_for_run(RunId("run_1")) == []


async def test_fake_event_bus_satisfies_protocol() -> None:
    bus: EventBus = FakeEventBus()

    async def handler(_event: object) -> None:
        return None

    await bus.subscribe("*", handler)


async def test_fake_attack_graph_store_satisfies_protocol() -> None:
    store: AttackGraphStore = FakeAttackGraphStore()
    assert await store.query() == []


def test_fake_llm_client_satisfies_protocol() -> None:
    client: LLMClient = FakeLLMClient()
    assert client is not None
