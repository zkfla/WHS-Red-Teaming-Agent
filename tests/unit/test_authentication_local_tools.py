"""Optional registered local auth tool over preserved request evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import EndpointTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.authentication_local_tools import (
    AUTHENTICATION_ASSESS_TOOL_ID,
    authentication_assess_handler,
    authentication_assessment_contribution,
)
from redteam.orchestration.events import AUTHENTICATION_CHECKED
from redteam.orchestration.local_tools import LocalToolContext, LocalToolRegistry
from tests.fakes.evidence_store import FakeEvidenceStore

RUN = "run-auth-local"
ENGAGEMENT = "eng-auth-local"
AGENT = "web-api-agent"
WEB_TOOL = "web.http_request"
HOST = "app.local"
TARGET_REF = "app"
PATH = "/"
SESSION = "alice"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal(
    *,
    action_id: str,
    target: Any,
    arguments: dict[str, Any],
    run_id: str = RUN,
    engagement_id: str = ENGAGEMENT,
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        agent_id=AgentId(AGENT),
        capability="web",
        operation="http_request",
        target=target,
        arguments=arguments,
        reason="preserve an http observation for later comparison",
        created_at=_now(),
    )


def _output(*, status: int, session_ref: str | None) -> dict[str, Any]:
    return {
        "target_ref": TARGET_REF,
        "session_ref": session_ref,
        "status_code": status,
        "body_bytes": 0,
        "body_sha256": ("b" if session_ref else "a") * 64,
        "redirect": False,
        "body_omitted": True,
    }


def _result(
    *, action_id: str, invocation_id: str, status: int, session_ref: str | None
) -> ToolResult:
    now = _now()
    return ToolResult(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId(action_id),
        tool_id=ToolId(WEB_TOOL),
        success=True,
        output=_output(status=status, session_ref=session_ref),
        started_at=now,
        finished_at=now,
    )


def _stale(*, status: int, session_ref: str | None) -> dict[str, Any]:
    return {
        "kind": "http_response",
        "method": "GET",
        "path": PATH,
        "target_ref": TARGET_REF,
        "session_ref": session_ref,
        "status_code": status,
    }


def _record(
    *,
    evidence_id: str,
    action_id: str,
    invocation_id: str,
    proposal: ActionProposal,
    result: ToolResult,
    target_node_id: str,
    observation: dict[str, Any],
    run_id: str = RUN,
    engagement_id: str = ENGAGEMENT,
    include_proposal: bool = True,
) -> EvidenceRecord:
    metadata: dict[str, Any] = {
        "success": True,
        "target_node_id": target_node_id,
        "agent_id": str(proposal.agent_id),
        "capability": proposal.capability,
        "operation": proposal.operation,
        "tool_id": WEB_TOOL,
        "action_id": action_id,
        "invocation_id": invocation_id,
        "tool_result": result.model_dump(mode="json"),
        "http_observation": observation,
    }
    if include_proposal:
        metadata["proposal"] = proposal.model_dump(mode="json")
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        source_type=EvidenceSourceType.TOOL,
        source_id=WEB_TOOL,
        action_id=ActionId(action_id),
        invocation_id=InvocationId(invocation_id),
        content_type="application/json",
        digest="00" * 32,
        summary="preserved http observation",
        created_at=_now(),
        metadata=metadata,
    )


async def _world() -> SimpleNamespace:
    graph_store = InMemoryAttackGraphStore()
    service = AttackGraphService(graph_store)
    host_node = await service.observe_target(HostTarget(host=HOST), step=0)
    endpoint_node = await service.observe_target(EndpointTarget(host=HOST, path=PATH), step=0)
    evidence_store = FakeEvidenceStore()
    bus = InMemoryEventBus()
    runtime = SimpleNamespace(run_id=RUN, engagement_id=ENGAGEMENT, step_count=3)
    deps = SimpleNamespace(evidence_store=evidence_store, graph_store=graph_store, event_bus=bus)
    context = LocalToolContext(deps=deps, runtime=runtime, purpose="test")
    return SimpleNamespace(
        service=service,
        host_node_id=str(host_node.id),
        endpoint_node_id=str(endpoint_node.id),
        evidence_store=evidence_store,
        bus=bus,
        runtime=runtime,
        deps=deps,
        context=context,
    )


def _parts(
    *, current_target: Any | None = None, current_args: dict[str, Any] | None = None
) -> tuple:
    baseline_arguments = {"target_ref": TARGET_REF, "path": PATH, "method": "GET"}
    session_arguments = current_args or {
        "target_ref": TARGET_REF,
        "path": PATH,
        "method": "GET",
        "session_ref": SESSION,
    }
    baseline_proposal = _proposal(
        action_id="action-base", target=HostTarget(host=HOST), arguments=baseline_arguments
    )
    session_proposal = _proposal(
        action_id="action-now",
        target=current_target or HostTarget(host=HOST),
        arguments=session_arguments,
    )
    baseline_result = _result(
        action_id="action-base", invocation_id="inv-base", status=401, session_ref=None
    )
    session_result = _result(
        action_id="action-now",
        invocation_id="inv-now",
        status=200,
        session_ref=session_arguments.get("session_ref"),
    )
    return (baseline_proposal, session_proposal, baseline_result, session_result)


async def _stored_pair(
    world: SimpleNamespace,
    *,
    current_target: Any | None = None,
    current_node: str | None = None,
    current_args: dict[str, Any] | None = None,
) -> tuple[str, str]:
    baseline_proposal, session_proposal, baseline_result, session_result = _parts(
        current_target=current_target, current_args=current_args
    )
    node_id = current_node or world.host_node_id
    baseline = _record(
        evidence_id="ev-base",
        action_id="action-base",
        invocation_id="inv-base",
        proposal=baseline_proposal,
        result=baseline_result,
        target_node_id=world.host_node_id,
        observation=_stale(status=500, session_ref=None),
    )
    current = _record(
        evidence_id="ev-now",
        action_id="action-now",
        invocation_id="inv-now",
        proposal=session_proposal,
        result=session_result,
        target_node_id=node_id,
        observation=_stale(status=418, session_ref=SESSION),
    )
    await world.evidence_store.append(baseline)
    await world.evidence_store.append(current)
    return ("ev-base", "ev-now")


async def _call(world: SimpleNamespace, **arguments: Any):
    registry = LocalToolRegistry()
    descriptor, handler = authentication_assessment_contribution().factory(None)
    assert descriptor.tool_id == AUTHENTICATION_ASSESS_TOOL_ID
    registry.register(descriptor, handler)
    return await registry.call(AUTHENTICATION_ASSESS_TOOL_ID, dict(arguments), world.context)


async def test_registered_tool_rejects_unknown_tool_id() -> None:
    world = await _world()
    registry = LocalToolRegistry()
    descriptor, handler = authentication_assessment_contribution().factory(None)
    registry.register(descriptor, handler)
    assert registry.get("no.such.tool") is None
    refused = await registry.call("no.such.tool", {}, world.context)
    assert refused.ok is False


async def test_exact_pair_stores_observation_event_and_cursor() -> None:
    world = await _world()
    baseline_id, session_id = await _stored_pair(world)
    result = await _call(
        world, baseline_evidence_id=baseline_id, session_evidence_id=session_id, session_ref=SESSION
    )
    assert result.ok is True
    assert result.refs == (baseline_id, session_id)
    assert result.payload["outcome"] == "session_access_observed"
    assert result.payload["anonymous_status"] == 401
    assert result.payload["response_status_code"] == 200
    assert "session_status" not in result.payload
    assert result.payload["path"] == PATH
    assert result.last_event_id is not None
    events = world.bus.history(event_type=AUTHENTICATION_CHECKED)
    assert len(events) == 1
    assert events[0].event_id == result.last_event_id
    assert events[0].payload["assessment_kind"] == "observation"
    assert events[0].payload["action_id"] == "action-now"
    assert events[0].payload["hypothesis_id"] is None
    assert events[0].payload["evidence_ids"] == [baseline_id, session_id]
    node = await world.service.get_node(GraphNodeId(world.host_node_id))
    assert node is not None
    checks = facts_of(node.properties).get("authentication_checks")
    assert isinstance(checks, list) and len(checks) == 1
    assert checks[0]["outcome"] == "session_access_observed"
    assert checks[0]["evidence_ids"] == [baseline_id, session_id]
    assert await world.service.list_findings() == []


async def test_foreign_run_evidence_is_refused() -> None:
    world = await _world()
    baseline_proposal, session_proposal, baseline_result, session_result = _parts()
    foreign_proposal = _proposal(
        action_id="action-base",
        target=HostTarget(host=HOST),
        arguments={"target_ref": TARGET_REF, "path": PATH, "method": "GET"},
        run_id="run-other",
    )
    foreign = _record(
        evidence_id="ev-foreign",
        action_id="action-base",
        invocation_id="inv-base",
        proposal=foreign_proposal,
        result=baseline_result,
        target_node_id=world.host_node_id,
        observation=_stale(status=401, session_ref=None),
        run_id="run-other",
    )
    current = _record(
        evidence_id="ev-now",
        action_id="action-now",
        invocation_id="inv-now",
        proposal=session_proposal,
        result=session_result,
        target_node_id=world.host_node_id,
        observation=_stale(status=200, session_ref=SESSION),
    )
    await world.evidence_store.append(foreign)
    await world.evidence_store.append(current)
    result = await authentication_assess_handler(
        {
            "baseline_evidence_id": "ev-foreign",
            "session_evidence_id": "ev-now",
            "session_ref": SESSION,
        },
        world.context,
    )
    assert result.ok is False
    assert "unavailable for run" in (result.error or "")
    assert world.bus.history(event_type=AUTHENTICATION_CHECKED) == []


async def test_unlinked_raw_evidence_is_refused() -> None:
    world = await _world()
    baseline_proposal, session_proposal, baseline_result, session_result = _parts()
    unlinked = _record(
        evidence_id="ev-base",
        action_id="action-base",
        invocation_id="inv-base",
        proposal=baseline_proposal,
        result=baseline_result,
        target_node_id=world.host_node_id,
        observation=_stale(status=401, session_ref=None),
        include_proposal=False,
    )
    current = _record(
        evidence_id="ev-now",
        action_id="action-now",
        invocation_id="inv-now",
        proposal=session_proposal,
        result=session_result,
        target_node_id=world.host_node_id,
        observation=_stale(status=200, session_ref=SESSION),
    )
    await world.evidence_store.append(unlinked)
    await world.evidence_store.append(current)
    result = await authentication_assess_handler(
        {
            "baseline_evidence_id": "ev-base",
            "session_evidence_id": "ev-now",
            "session_ref": SESSION,
        },
        world.context,
    )
    assert result.ok is False
    assert "origin is incomplete" in (result.error or "")
    assert world.bus.history(event_type=AUTHENTICATION_CHECKED) == []


@pytest.mark.parametrize("mismatch", ["path", "target", "session"])
async def test_baseline_mismatch_stays_inconclusive(mismatch: str) -> None:
    world = await _world()
    arguments: dict[str, Any] = {
        "target_ref": TARGET_REF,
        "path": PATH,
        "method": "GET",
        "session_ref": SESSION,
    }
    if mismatch == "path":
        arguments = {**arguments, "path": "/other"}
    elif mismatch == "target":
        arguments = {**arguments, "target_ref": "other"}
    else:
        arguments = {**arguments, "session_ref": "mallory"}
    baseline_id, session_id = await _stored_pair(world, current_args=arguments)
    result = await _call(
        world, baseline_evidence_id=baseline_id, session_evidence_id=session_id, session_ref=SESSION
    )
    assert result.ok is True
    assert result.payload["outcome"] == "baseline_evidence_unavailable_or_mismatched"
    assert result.payload["evidence_ids"] == [session_id]
    assert result.refs == (session_id,)
    assert await world.service.list_findings() == []


async def test_host_baseline_to_endpoint_current_compares_by_network_identity() -> None:
    world = await _world()
    baseline_id, session_id = await _stored_pair(
        world,
        current_target=EndpointTarget(host=HOST, path=PATH),
        current_node=world.endpoint_node_id,
    )
    result = await _call(
        world, baseline_evidence_id=baseline_id, session_evidence_id=session_id, session_ref=SESSION
    )
    assert result.ok is True
    assert result.refs == (baseline_id, session_id)
    assert result.payload["outcome"] == "session_access_observed"
    assert result.payload["response_status_code"] == 200
    assert "session_status" not in result.payload
    assert result.payload["evidence_ids"] == [baseline_id, session_id]
    node = await world.service.get_node(GraphNodeId(world.endpoint_node_id))
    assert node is not None
    checks = facts_of(node.properties).get("authentication_checks")
    assert isinstance(checks, list) and len(checks) == 1
    assert checks[0]["outcome"] == "session_access_observed"
    assert await world.service.list_findings() == []
