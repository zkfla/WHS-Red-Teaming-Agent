"""Stage 6 policy-denial and Task semantics (Prompt 6).

A Control ``DENY`` is a *decision*, never a tool failure. Orchestration must:

* record the block without touching ``consecutive_failures`` / retry counters
  (EXECUTE owns those; a block never reached EXECUTE);
* react to Control's severity hint, carried in
  ``PolicyDecision.matched_rules`` as ``"severity:<level>"`` because the frozen
  contract has no dedicated field:
    - ``BLOCK_ACTION``  -> block only this action; replan toward the same goal;
    - ``REPLAN_GOAL``   -> LLM replans (task stays RUNNING + feedback);
      no automatic task blocking or goal switching in code;
    - ``ABORT_RUN``     -> nothing more can proceed; end the run with a named
      reason;
* survive a denial whose active Task is already settled -- a REJECTED approval
  can arrive after the work was closed elsewhere and must replan, not crash.
* policy admission needs an active path; tests register a matching active
  PathPlan/PathStep so the real policy path runs (no legacy fallback).
* denial evidence is the work ledger (BLOCKED) and bus events
  (ACTION_DENIED/GRAPH_UPDATED), not graph facts.

The fan-out severity hand-off is exercised separately in
``test_stage6_fanout_policy_semantics``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.control.severity import Severity, to_tag
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import (
    AgentPhase,
    GraphNodeType,
    MissionStatus,
    PolicyVerdict,
    TaskStatus,
)
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    EngagementId,
    GraphNodeId,
    MissionId,
    RunId,
    TaskId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import PlanStep
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.nodes import (
    observe_node,
    policy_node,
    replan_node,
    update_node,
)
from redteam.orchestration.nodes.policy import denial_severity
from redteam.orchestration.paths import PathPlan, PathStep
from redteam.orchestration.router import route_after_policy
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
    resume_graph_state,
)
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes import FakeControlPlane, build_test_application

NOW = datetime.now(timezone.utc)
RUN = RunId("run_stage6_policy")
ENG = EngagementId("eng_stage6_policy")
MISSION = MissionId("mission_stage6_policy")


def runtime(**overrides: Any) -> RuntimeState:
    return RuntimeState(run_id=RUN, engagement_id=ENG, **overrides)


def proposal(
    action_id: str = "action_policy",
    *,
    agent_id: str = "recon-agent",
    capability: str = "recon",
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN,
        engagement_id=ENG,
        agent_id=AgentId(agent_id),
        capability=capability,
        operation="service_discovery",
        target=HostTarget(host="a.local"),
        reason="stage6",
        created_at=NOW,
    )


def decision(
    verdict: PolicyVerdict = PolicyVerdict.DENY,
    severity: Severity | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        action_id=ActionId("action_policy"),
        verdict=verdict,
        reasons=["stage6"],
        matched_rules=[to_tag(severity)] if severity is not None else [],
        evaluated_at=NOW,
    )


class _TaggedPlane(FakeControlPlane):
    """A fake Control Plane that tags every verdict with a severity level."""

    def __init__(self, verdict: PolicyVerdict, severity: Severity | None = None) -> None:
        super().__init__(default_verdict=verdict)
        self._severity = severity

    async def evaluate(self, proposal, context):  # noqa: ANN001, ANN201
        self.evaluated.append(proposal)
        return PolicyDecision(
            action_id=proposal.action_id,
            verdict=self.default_verdict,
            reasons=["tagged"],
            matched_rules=[to_tag(self._severity)] if self._severity else [],
            approval_id=(
                ApprovalId(f"appr_{proposal.action_id}")
                if self.default_verdict is PolicyVerdict.REQUIRE_APPROVAL
                else None
            ),
            evaluated_at=NOW,
        )


def _mission() -> MissionPlan:
    return MissionPlan(
        mission_id=MISSION,
        engagement_id=ENG,
        objective="stage6 policy semantics",
        status=MissionStatus.ACTIVE,
        provenance=Provenance(source="test"),
        created_at=NOW,
        updated_at=NOW,
    )


def _task(
    task_id: str,
    *,
    capability: str = "recon",
    status: TaskStatus = TaskStatus.READY,
    priority: int = 100,
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MISSION,
        title=task_id,
        objective=f"perform {task_id}",
        status=status,
        priority=priority,
        required=True,
        required_capability=capability,
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


def _bound_plan_step(
    *,
    path_id: str,
    version: int,
    path_step_id: str,
    host: str = "a.local",
) -> PlanStep:
    return PlanStep(
        step_id=f"step_{path_step_id}",
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.probe"),
        target=HostTarget(host=host),
        reason="stage6",
        path_id=path_id,
        path_version=version,
        path_step_id=path_step_id,
    )


async def _register_active_path(
    deps, run_id, step_hosts: dict, *, path_id: str = "path_stage6_active"
):
    steps = [
        PathStep(
            step_id=step_id,
            objective=f"probe {host}",
            target_node_ids=[f"host:{host}"],
            allowed_operations=["recon.service_discovery"],
        )
        for step_id, host in step_hosts.items()
    ]
    plan = PathPlan(
        path_id=path_id,
        run_id=str(run_id),
        version=1,
        status="active",
        reason="stage6 active path",
        goal="stage6",
        steps=steps,
    )
    await deps.path_store.register(plan)
    return path_id, 1


async def _deps_with_tasks(
    tasks: list[MissionTask],
    *,
    plane: FakeControlPlane | None = None,
    bus: InMemoryEventBus | None = None,
):
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())
    for item in tasks:
        await store.create_task(item)
    deps = replace(
        build_test_application(
            control_plane=plane or FakeControlPlane(),
            event_bus=bus or InMemoryEventBus(),
        ),
        task_graph_store=store,
    )
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(store, deps.path_store).bind(str(RUN), str(MISSION), str(ENG))
    # ``observe_node`` only surfaces an agent when the role can see something.
    await AttackGraphService(deps.graph_store).upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"in_scope": True}
    )
    return deps


# -- severity tag reader -------------------------------------------------


def test_denial_severity_reads_every_control_tag() -> None:
    for severity in Severity:
        tagged = decision(PolicyVerdict.DENY, severity)
        assert denial_severity(tagged) == severity.value


def test_denial_severity_is_none_when_untagged_or_malformed() -> None:
    assert denial_severity(None) is None
    assert denial_severity(decision(PolicyVerdict.DENY, None)) is None
    assert denial_severity(decision(PolicyVerdict.ALLOW, Severity.ABORT_RUN)) is None
    malformed = PolicyDecision(
        action_id=ActionId("a"),
        verdict=PolicyVerdict.DENY,
        matched_rules=["severity:not_a_level"],
        evaluated_at=NOW,
    )
    # Unknown level strings are surfaced verbatim; only the *matching* levels
    # in REPLAN cause a reaction, so an unknown one degrades to "replan".
    assert denial_severity(malformed) == "not_a_level"


# -- denial is a block, not a tool failure -------------------------------


async def test_denial_is_recorded_as_a_block_not_a_tool_failure() -> None:
    plane = _TaggedPlane(PolicyVerdict.DENY, Severity.BLOCK_ACTION)
    bus = InMemoryEventBus()
    deps = build_test_application(control_plane=plane, event_bus=bus)
    path_id, version = await _register_active_path(deps, RUN, {"pstep_policy": "a.local"})
    step = _bound_plan_step(path_id=path_id, version=version, path_step_id="pstep_policy")
    held = proposal()
    work_id = "work_stage6_block"
    now = datetime.now(timezone.utc)
    await deps.work_store.add(
        WorkItem(
            work_id=work_id,
            run_id=RUN,
            candidate_key=work_id,
            agent_id=held.agent_id,
            target_node_ids=[GraphNodeId("host:a.local")],
            priority=PriorityScore(total=1),
            created_at=now,
            updated_at=now,
        )
    )
    await deps.work_store.acquire(work_id, owner=str(RUN), now=now, ttl=timedelta(seconds=60))
    state = initial_graph_state(
        runtime(
            consecutive_failures=2,
            retry_count=1,
            retry_tool_calls=5,
            actions_executed=3,
            remaining_budget=4,
        )
    )
    state["action_proposal"] = held
    state["plan_step"] = step
    state["current_work_id"] = work_id

    evaluated = await policy_node(state, deps=deps)
    assert evaluated["tool_result"] is None
    assert route_after_policy(evaluated) == "update"
    assert evaluated["policy_decision"] is not None
    assert evaluated["policy_decision"].verdict is PolicyVerdict.DENY
    assert "severity:block_action" in evaluated["policy_decision"].matched_rules

    updated = await update_node(evaluated, deps=deps)
    rt = updated["runtime"]

    # Nothing executed, so no failure/retry/budget accounting moved.
    assert rt.consecutive_failures == 2
    assert rt.retry_count == 1
    assert rt.retry_tool_calls == 5
    assert rt.actions_executed == 3
    assert rt.remaining_budget == 4

    types = bus.event_types()
    assert EventType.ACTION_DENIED in types
    assert not {
        EventType.TOOL_EXECUTION_STARTED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.TOOL_EXECUTION_FAILED,
    } & set(types)
    stored_work = await deps.work_store.get(work_id)
    assert stored_work is not None and stored_work.status is WorkStatus.BLOCKED
    assert stored_work.outcome_reason == "deny"
    denied = bus.history(event_type=EventType.ACTION_DENIED)[-1]
    assert str(denied.payload["action_id"]) == str(held.action_id)
    assert denied.payload["reasons"]

    graph_updates = [
        e for e in bus.history(event_type=EventType.GRAPH_UPDATED) if e.payload.get("blocked")
    ]
    assert graph_updates
    assert graph_updates[-1].payload["block_verdict"] == "deny"
    assert str(graph_updates[-1].payload["action_id"]) == str(held.action_id)


# -- severity-driven reaction in REPLAN ----------------------------------


async def test_block_action_denial_replans_toward_the_same_goal() -> None:
    bus = InMemoryEventBus()
    task = _task("task_recon")
    deps = await _deps_with_tasks([task], bus=bus)
    state = await observe_node(initial_graph_state(runtime(mission_id=MISSION)), deps=deps)
    assert state["runtime"].active_task_id == task.task_id

    state = {
        **state,
        "action_proposal": proposal(),
        "policy_decision": decision(PolicyVerdict.DENY, Severity.BLOCK_ACTION),
    }
    result = await replan_node(state, deps=deps)  # type: ignore[arg-type]

    # The action was blocked, but the goal is still pursued: the Task stays
    # RUNNING and the loop continues.
    assert result["runtime"].phase is AgentPhase.OBSERVE
    stored = await deps.task_graph_store.get_task(task.task_id)
    assert stored is not None and stored.status is TaskStatus.RUNNING
    assert EventType.TASK_BLOCKED not in bus.event_types()


async def test_replan_goal_denial_keeps_task_running_for_llm_replan() -> None:
    bus = InMemoryEventBus()
    blocked = _task("task_rejected", priority=100)
    other = _task("task_other", priority=50)
    deps = await _deps_with_tasks([blocked, other], bus=bus)
    state = await observe_node(initial_graph_state(runtime(mission_id=MISSION)), deps=deps)
    assert state["runtime"].active_task_id == blocked.task_id

    state = {
        **state,
        "action_proposal": proposal(),
        "policy_decision": decision(PolicyVerdict.DENY, Severity.REPLAN_GOAL),
    }
    result = await replan_node(state, deps=deps)  # type: ignore[arg-type]

    # REPLAN_GOAL no longer auto-blocks: LLM replans, task stays RUNNING + feedback.
    rejected = await deps.task_graph_store.get_task(blocked.task_id)
    assert rejected is not None and rejected.status is TaskStatus.RUNNING
    untried = await deps.task_graph_store.get_task(other.task_id)
    assert untried is not None and untried.status is not TaskStatus.BLOCKED
    assert result["runtime"].phase is AgentPhase.OBSERVE
    assert result["runtime"].active_task_id == blocked.task_id
    assert "stage6" in result["runtime"].planning_feedback
    assert EventType.TASK_BLOCKED not in bus.event_types()


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        (["scope", "severity:abort_run"], TerminationReason.MISSION_BLOCKED),
        (["budget", "severity:abort_run"], TerminationReason.BUDGET_EXHAUSTED),
    ],
)
async def test_abort_run_denial_ends_the_run_with_a_named_reason(
    rules: list[str], expected: str
) -> None:
    deps = build_test_application(event_bus=InMemoryEventBus())
    abort = PolicyDecision(
        action_id=ActionId("action_policy"),
        verdict=PolicyVerdict.DENY,
        reasons=["nothing more can proceed"],
        matched_rules=rules,
        evaluated_at=NOW,
    )
    state = {
        **initial_graph_state(runtime(consecutive_failures=1)),
        "action_proposal": proposal(),
        "policy_decision": abort,
    }

    result = await replan_node(state, deps=deps)  # type: ignore[arg-type]

    assert result["runtime"].phase is AgentPhase.FINISHED
    assert result["runtime"].is_terminal
    assert result["runtime"].termination_reason == expected
    # Still not an execution failure.
    assert result["runtime"].consecutive_failures == 1


async def test_replan_goal_denial_with_a_settled_task_does_not_crash() -> None:
    """A REJECTED approval can arrive after the Task was closed elsewhere."""
    bus = InMemoryEventBus()
    settled = _task("task_done")
    deps = await _deps_with_tasks([settled], bus=bus)
    await deps.task_graph_store.transition_task(settled.task_id, TaskStatus.RUNNING, now=NOW)
    await deps.task_graph_store.transition_task(settled.task_id, TaskStatus.COMPLETED, now=NOW)
    state = {
        **initial_graph_state(runtime(mission_id=MISSION, active_task_id=settled.task_id)),
        "action_proposal": proposal(),
        "policy_decision": decision(PolicyVerdict.DENY, Severity.REPLAN_GOAL),
    }

    result = await replan_node(state, deps=deps)  # type: ignore[arg-type]

    # No invalid transition, and the settled Task is untouched.
    stored = await deps.task_graph_store.get_task(settled.task_id)
    assert stored is not None and stored.status is TaskStatus.COMPLETED
    assert EventType.TASK_BLOCKED not in bus.event_types()
    assert result["runtime"].phase is not None


# -- the approval-deny resume actually replans ---------------------------


async def test_approval_rejection_replans_and_keeps_task_running() -> None:
    """POLICY(resumed) -> UPDATE -> REPLAN for a rejected approval.

    Mirrors ``AgentLoopRunner``'s resume seeding: the runtime is already at
    POLICY with the held proposal, and Control's resolved outcome is a DENY
    tagged ``REPLAN_GOAL`` (how ``DefaultControlPlane.approval_outcome`` marks
    a rejection).
    """
    bus = InMemoryEventBus()
    task = _task("task_held")
    plane = _TaggedPlane(PolicyVerdict.REQUIRE_APPROVAL)
    deps = await _deps_with_tasks([task], plane=plane, bus=bus)
    await deps.task_graph_store.transition_task(task.task_id, TaskStatus.RUNNING, now=NOW)

    path_id, version = await _register_active_path(deps, RUN, {"pstep_held": "a.local"})
    step = _bound_plan_step(path_id=path_id, version=version, path_step_id="pstep_held")

    held = proposal()
    approval_id = ApprovalId("appr_held")
    plane.resolve_approval(
        approval_id,
        PolicyDecision(
            action_id=held.action_id,
            verdict=PolicyVerdict.DENY,
            reasons=[f"approval {approval_id} was rejected, not approved"],
            matched_rules=["approval", to_tag(Severity.REPLAN_GOAL)],
            approval_id=approval_id,
            evaluated_at=NOW,
        ),
    )
    resume_runtime = runtime(
        mission_id=MISSION,
        active_task_id=task.task_id,
        phase=AgentPhase.POLICY,
        termination_reason=TerminationReason.APPROVAL_PENDING,
        pending_action_id=held.action_id,
        pending_approval_id=approval_id,
    )
    state: GraphState = resume_graph_state(resume_runtime, action_proposal=held, plan_step=step)

    state = await policy_node(state, deps=deps)
    state = await update_node(state, deps=deps)
    result = await replan_node(state, deps=deps)

    assert result["runtime"].pending_action_id is None
    assert result["runtime"].pending_approval_id is None
    assert result["runtime"].termination_reason != TerminationReason.APPROVAL_PENDING
    assert result["runtime"].phase is AgentPhase.OBSERVE
    rejected = await deps.task_graph_store.get_task(task.task_id)
    assert rejected is not None and rejected.status is TaskStatus.RUNNING
    assert EventType.TASK_BLOCKED not in bus.event_types()
    assert EventType.ACTION_DENIED in bus.event_types()
    assert any("was rejected" in reason for reason in result["runtime"].planning_feedback)
