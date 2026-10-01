"""EXECUTE-layer safety controls (Prompt 6).

Covers the controls that only bite at the tool call site:

  - ``RuntimeDeps.tool_semaphore`` — the actual
    ``RateLimitPolicy.max_concurrent_actions`` execution gate.
  - bounded retry of a *retryable, known-low-risk* failure, capped by
    ``OrchestrationSettings.max_retries`` and accounted against budget.
  - ``verification_reserve_ratio`` budget scheduling (opt-in).
  - policy denial never becomes a tool failure (it never reaches EXECUTE).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from redteam.attack_graph.identity import host_node_id
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.enums import PolicyVerdict, RiskLevel
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId, ToolId
from redteam.orchestration.agents import PlanStep
from redteam.orchestration.nodes import execute_node, policy_node
from redteam.orchestration.nodes.execute import _retry_safe
from redteam.orchestration.paths.models import CompletionCriterion, PathPlan
from redteam.orchestration.paths.models import PathStep as RegisteredStep
from redteam.orchestration.router import route_after_policy
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import GraphState, RuntimeState, initial_graph_state
from tests.fakes import FakeControlPlane, build_test_application
from tests.fakes.tool_gateway import DummyToolGateway

NOW = datetime.now(timezone.utc)
RUN = RunId("run_exec_safety")
ENGAGEMENT = EngagementId("eng_exec_safety")
_PATH_ID = "path-exec-safety"
_PATH_STEP_ID = "probe-host"


def runtime(**overrides) -> RuntimeState:
    return RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT, **overrides)


def proposal(action_id: str = "action_exec") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId("recon-agent"),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host="a.local"),
        reason="probe",
        created_at=NOW,
    )


def descriptor(risk: RiskLevel) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("recon.probe"),
        server_id="srv",
        tool_name="probe",
        capability_tags=["recon"],
        risk_level=risk,
    )


async def _ensure_exec_path(deps) -> None:
    existing = await deps.path_store.get_active(str(RUN))
    if existing is not None:
        return
    plan = PathPlan(
        path_id=_PATH_ID,
        run_id=str(RUN),
        mission_id=None,
        version=1,
        parent_version=None,
        status="active",
        reason="test fixture path",
        goal="probe",
        steps=[
            RegisteredStep(
                step_id=_PATH_STEP_ID,
                objective="probe host",
                agent_id="recon-agent",
                target_node_ids=[str(host_node_id("a.local"))],
                principal_refs=[],
                allowed_operations=["recon.service_discovery", "recon.auth_attempt"],
                depends_on=[],
                required_fact_refs=[],
                unknowns=[],
                completion_criteria=[
                    CompletionCriterion(kind="evidence", description="probe", min_evidence=1)
                ],
                required=True,
                status="pending",
            )
        ],
        hypothesis_ids=[],
        evidence_ids=[],
        changed_step_ids=[],
        review_records={},
    )
    try:
        await deps.path_store.register(plan)
    except Exception:
        pass


def _exec_plan_step(prop: ActionProposal) -> PlanStep:
    return PlanStep(
        step_id=_PATH_STEP_ID,
        path_id=_PATH_ID,
        path_version=1,
        path_step_id=_PATH_STEP_ID,
        capability=prop.capability,
        operation=prop.operation,
        tool_id=ToolId("recon.probe"),
        target=prop.target,
        arguments=dict(prop.arguments),
        reason="probe",
        availability_impact=prop.availability_impact,
    )


async def execute(
    deps,
    *,
    action_id: str = "action_exec",
    remaining_budget: int | None = 5,
    actions_executed: int = 0,
) -> GraphState:
    await _ensure_exec_path(deps)
    state = initial_graph_state(
        runtime(
            remaining_budget=remaining_budget,
            actions_executed=actions_executed,
        )
    )
    prop = proposal(action_id)
    state["action_proposal"] = prop
    state["plan_step"] = _exec_plan_step(prop)
    state = await policy_node(state, deps=deps)
    return await execute_node(state, deps=deps)


class _BlockingGateway:
    """Blocks every invocation until released, tracking peak overlap."""

    def __init__(self) -> None:
        self.invocations: list[ToolInvocation] = []
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        occurred_at = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=True,
            output={},
            started_at=occurred_at,
            finished_at=occurred_at,
        )


class _FlakyGateway(DummyToolGateway):
    """Fails ``fail_times`` times with a (by default) retryable error."""

    def __init__(
        self,
        *,
        fail_times: int,
        risk: RiskLevel = RiskLevel.LOW,
        retryable: bool = True,
    ) -> None:
        super().__init__(tools=[descriptor(risk)])
        self.fail_times = fail_times
        self.retryable = retryable
        self.attempts = 0

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            self.invocations.append(invocation)
            occurred_at = datetime.now(timezone.utc)
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                error=ToolError(code="tool_timeout", message="slow", retryable=self.retryable),
                started_at=occurred_at,
                finished_at=occurred_at,
            )
        return await super().invoke(invocation)


# --- max_concurrent_actions execution gate -----------------------------


async def test_tool_semaphore_bounds_concurrent_tool_calls() -> None:
    gateway = _BlockingGateway()
    deps = replace(
        build_test_application(tool_gateway=gateway), tool_semaphore=asyncio.Semaphore(1)
    )

    first = asyncio.create_task(execute(deps, action_id="action_a"))
    second = asyncio.create_task(execute(deps, action_id="action_b"))
    await asyncio.sleep(0.05)
    assert gateway.max_active == 1  # the second call is gated, not running

    gateway.release.set()
    await asyncio.gather(first, second)
    assert len(gateway.invocations) == 2
    assert gateway.max_active == 1


async def test_no_semaphore_leaves_execution_unbounded() -> None:
    gateway = _BlockingGateway()
    deps = build_test_application(tool_gateway=gateway)  # tool_semaphore defaults None

    first = asyncio.create_task(execute(deps, action_id="action_a"))
    second = asyncio.create_task(execute(deps, action_id="action_b"))
    await asyncio.sleep(0.05)
    assert gateway.max_active == 2

    gateway.release.set()
    await asyncio.gather(first, second)


# --- bounded retry ------------------------------------------------------


async def test_retryable_low_risk_failure_is_retried_and_recovers() -> None:
    gateway = _FlakyGateway(fail_times=1)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=3),
    )

    state = await execute(deps, remaining_budget=5)
    result = state["tool_result"]
    assert result is not None and result.success is True
    assert len(gateway.invocations) == 2
    assert result.metadata["retry_attempts"] == 1
    assert state["runtime"].retry_count == 1
    # One action + one retry charged against the local budget mirror.
    assert state["runtime"].remaining_budget == 3


async def test_retry_is_capped_by_max_retries() -> None:
    gateway = _FlakyGateway(fail_times=10)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=2),
    )

    state = await execute(deps)
    result = state["tool_result"]
    assert result is not None and result.success is False
    assert len(gateway.invocations) == 3  # initial attempt + 2 retries
    assert state["runtime"].retry_count == 2


async def test_retry_skipped_for_non_low_risk_tool() -> None:
    gateway = _FlakyGateway(fail_times=10, risk=RiskLevel.HIGH)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=3),
    )

    state = await execute(deps)
    result = state["tool_result"]
    assert result is not None and result.success is False
    assert len(gateway.invocations) == 1
    assert state["runtime"].retry_count == 0


async def test_low_risk_state_changing_operation_is_not_retried() -> None:
    gateway = _FlakyGateway(fail_times=10, risk=RiskLevel.LOW)
    deps = build_test_application(
        tool_gateway=gateway, tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=3),
    )
    await _ensure_exec_path(deps)
    state = initial_graph_state(runtime(remaining_budget=5))
    prop = proposal().model_copy(update={"operation": "auth_attempt"})
    state["action_proposal"] = prop
    state["plan_step"] = _exec_plan_step(prop)
    state = await policy_node(state, deps=deps)

    result = await execute_node(state, deps=deps)

    tool_result = result["tool_result"]
    assert tool_result is not None and tool_result.success is False
    assert len(gateway.invocations) == 1
    assert result["runtime"].retry_count == 0


def test_http_retry_requires_an_explicit_read_only_method() -> None:
    web = proposal().model_copy(update={"capability": "web", "operation": "http_request"})
    assert not _retry_safe(web)
    assert _retry_safe(web.model_copy(update={"arguments": {"method": "GET"}}))
    assert not _retry_safe(web.model_copy(update={"arguments": {"method": "POST"}}))
    assert not _retry_safe(
        web.model_copy(update={"arguments": {"method": "GET", "body": "payload"}})
    )


async def test_non_retryable_failure_is_not_retried() -> None:
    gateway = _FlakyGateway(fail_times=1, retryable=False)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=3),
    )

    await execute(deps)
    assert len(gateway.invocations) == 1


async def test_retry_can_be_disabled_by_settings() -> None:
    gateway = _FlakyGateway(fail_times=1)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_retries=3, retry_retryable_tool_failures=False),
    )

    await execute(deps)
    assert len(gateway.invocations) == 1


# --- verification reserve ----------------------------------------------


def test_verification_reserve_helper_is_bounded() -> None:
    settings = OrchestrationSettings(verification_reserve_ratio=0.2)
    assert settings.verification_reserve(10) == 2
    assert settings.verification_reserve(1) == 0  # never starve a single action
    assert settings.verification_reserve(0) == 0
    assert OrchestrationSettings(verification_reserve_ratio=0.0).verification_reserve(10) == 0


async def test_verification_reserve_stops_at_the_reserved_floor() -> None:
    deps = build_test_application(
        settings=OrchestrationSettings(
            enforce_verification_reserve=True, verification_reserve_ratio=0.2
        )
    )
    # total budget = actions_executed(7) + remaining(3) = 10 -> reserve 2.
    state = await execute(deps, remaining_budget=3, actions_executed=7)
    assert state["runtime"].remaining_budget == 0


async def test_verification_reserve_is_inert_by_default() -> None:
    deps = build_test_application()  # enforce flags default off
    state = await execute(deps, remaining_budget=3, actions_executed=7)
    assert state["runtime"].remaining_budget == 2


# --- policy denial never becomes a tool failure ------------------------


async def test_policy_denial_routes_away_and_never_invokes_the_tool() -> None:
    gateway = DummyToolGateway()
    deps = build_test_application(
        tool_gateway=gateway,
        control_plane=FakeControlPlane(default_verdict=PolicyVerdict.DENY),
    )
    state = initial_graph_state(runtime(remaining_budget=5))
    state["action_proposal"] = proposal()

    evaluated = await policy_node(state, deps=deps)

    assert evaluated["policy_decision"] is not None
    assert evaluated["policy_decision"].verdict == PolicyVerdict.DENY
    assert route_after_policy(evaluated) == "update"
    assert evaluated["tool_result"] is None
    assert gateway.invocations == []
