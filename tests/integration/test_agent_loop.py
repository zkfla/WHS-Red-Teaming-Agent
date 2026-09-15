"""The re-plan loop end to end: multi-turn, multi-agent, and replayable.

Complements ``test_end_to_end.py`` (one happy-path turn, every real
component) by exercising what the loop itself adds: iterating until the
frontier is empty, handing over between agents, stopping for a named
reason, never reaching the Capability Layer without an ALLOW, and being
able to rewind to an earlier checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.predicates import attrs_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase, GraphNodeType, PolicyVerdict, Verdict
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId, new_id
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import AgentRegistry, AgentSpec, PlanStep
from redteam.orchestration.agents.registry import RECON_AGENT
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application


def engagement(*hosts: str, max_actions: int = 10, denied: list[str] | None = None) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_loop"),
        name="loop",
        scope=EngagementScope(allowed_hosts=list(hosts), denied_hosts=denied or []),
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def bus(deps) -> InMemoryEventBus:
    """Every test here wires the in-memory backend, which also records history."""
    return cast(InMemoryEventBus, deps.event_bus)


async def run_with(deps, target: Engagement) -> RuntimeState:
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    return await runner.run(
        RuntimeState(
            run_id=RunId(new_id("run")),
            engagement_id=target.engagement_id,
            remaining_budget=target.execution_budget.max_actions,
        )
    )


# -- the loop actually loops ---------------------------------------------


async def test_the_loop_probes_every_in_scope_host_exactly_once(tmp_path) -> None:
    target = engagement("a.local", "b.local", "c.local")
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)

    final = await run_engagement(target, deps=deps)

    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    assert final.actions_executed == 3
    assert final.remaining_budget == 10 - 3

    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert len(evidence) == 3

    service = AttackGraphService(deps.graph_store)
    hosts = await service.query(node_type=GraphNodeType.HOST)
    assert {str(node.id) for node in hosts} == {
        "host:a.local",
        "host:b.local",
        "host:c.local",
    }
    assert all(attrs_of(node.properties)["reachable"] is True for node in hosts)

    # One probe per host, and no repeats: the attempt record in the graph
    # is what makes the frontier shrink.
    proposed = bus(deps).history(event_type=EventType.ACTION_PROPOSED)
    assert len(proposed) == 3
    assert len({e.payload["target"] for e in proposed}) == 3


async def test_a_denied_host_is_never_seeded_and_never_probed(tmp_path) -> None:
    target = engagement("a.local", "b.local", denied=["b.local"])
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)

    final = await run_engagement(target, deps=deps)

    assert final.actions_executed == 1
    service = AttackGraphService(deps.graph_store)
    assert {str(n.id) for n in await service.query(node_type=GraphNodeType.HOST)} == {
        "host:a.local"
    }


async def test_findings_stay_empty_and_the_graph_stays_reportable(tmp_path) -> None:
    target = engagement("a.local")
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)

    await run_engagement(target, deps=deps)

    service = AttackGraphService(deps.graph_store)
    # The example tool echoes, so the reachability expectation is met and
    # benign: PASS, no finding invented.
    assert await service.list_findings() == []
    report = await service.integrity_report()
    assert report.blocking == []
    assert report.unresolved_hypotheses == []


# -- stop conditions ------------------------------------------------------


async def test_the_turn_ceiling_stops_the_loop() -> None:
    deps = build_test_application(settings=OrchestrationSettings(max_steps=2, checkpointing=False))
    final = await run_with(deps, engagement("a.local", "b.local", "c.local"))

    assert final.termination_reason == TerminationReason.MAX_STEPS_REACHED
    assert final.step_count == 2


async def test_the_action_budget_stops_the_loop() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    final = await run_with(deps, engagement("a.local", "b.local", "c.local", max_actions=2))

    assert final.termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert final.actions_executed == 2


async def test_a_target_that_keeps_failing_is_not_hammered() -> None:
    deps = build_test_application(
        tool_gateway=DummyToolGateway(success=False),
        settings=OrchestrationSettings(max_consecutive_failures=2, checkpointing=False),
    )
    final = await run_with(deps, engagement("a.local", "b.local", "c.local"))

    assert final.termination_reason == TerminationReason.CONSECUTIVE_FAILURES
    assert final.actions_executed == 2
    assert final.last_verdict is Verdict.UNKNOWN


# -- the Control Plane gate ----------------------------------------------


@pytest.mark.parametrize(
    "verdict", [PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL]
)
async def test_nothing_reaches_the_capability_layer_without_an_allow(verdict) -> None:
    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=FakeControlPlane(default_verdict=verdict),
        tool_gateway=gateway,
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))

    assert gateway.invocations == []
    assert final.actions_executed == 0
    assert final.phase is AgentPhase.FINISHED
    assert EventType.TOOL_EXECUTION_STARTED not in bus(deps).event_types()


async def test_a_denied_action_still_retires_the_agent_rather_than_spinning() -> None:
    # The attempt is not recorded (nothing ran), so what ends the run is
    # the agent having no further steps, not an infinite retry.
    deps = build_test_application(
        control_plane=FakeControlPlane(default_verdict=PolicyVerdict.DENY),
        settings=OrchestrationSettings(max_steps=8, checkpointing=False),
    )
    final = await run_with(deps, engagement("a.local"))
    assert final.step_count < 8


# -- multi-agent scheduling ----------------------------------------------


async def test_agents_run_in_order_and_share_one_graph() -> None:
    follow_up = AgentSpec(
        agent_id=AgentId("follow-up-agent"),
        name="Follow-up Agent",
        playbook=[
            PlanStep(
                step_id="re-probe",
                capability="recon.followup",
                operation="ping",
                tool_id=ToolId("example.ping"),
                target_bind="host",
                preconditions=[
                    # Only offered for hosts the recon agent already
                    # proved reachable — one agent's output is the next
                    # one's frontier.
                    RECON_AGENT.playbook[0].preconditions[0].model_copy(
                        deep=True, update={"attrs": {"reachable": True}}
                    )
                ],
                reason="second look at a host that answered",
            )
        ],
    )
    deps = build_test_application(
        agents=AgentRegistry([RECON_AGENT, follow_up]),
        settings=OrchestrationSettings(max_steps=12, checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))

    assert final.finished_agents == [RECON_AGENT.agent_id, follow_up.agent_id]
    assert final.actions_executed == 2
    started = [
        e.payload["agent_id"]
        for e in bus(deps).history(event_type=EventType.AGENT_STARTED)
    ]
    assert started == ["recon-agent", "follow-up-agent"]


# -- checkpointing and replay --------------------------------------------


async def test_every_turn_is_checkpointed_and_can_be_replayed() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=True))
    target = engagement("a.local", "b.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)

    runner = AgentLoopRunner(deps, settings=deps.settings)
    final = await runner.run(
        RuntimeState(
            run_id=RunId("run_replay"),
            engagement_id=target.engagement_id,
            remaining_budget=target.execution_budget.max_actions,
        )
    )
    assert final.actions_executed == 2

    history = await runner.state_history()
    assert len(history) > 1
    assert history[0].phase is AgentPhase.FINISHED
    # Snapshots are newest-first, so step_count is non-increasing.
    assert [snapshot.step_count for snapshot in history] == sorted(
        (s.step_count for s in history), reverse=True
    )

    earlier = next(s for s in reversed(history) if s.actions_executed == 1 and s.next_nodes)
    resumed = await runner.resume_from(earlier.checkpoint_id)

    # Rewinding rewinds GraphState only: the Attack Graph kept the
    # recorded attempts, so the replayed branch finds a smaller frontier
    # rather than redoing work. Evidence is append-only by design.
    assert resumed.phase is AgentPhase.FINISHED
    assert resumed.actions_executed >= earlier.actions_executed


async def test_history_and_replay_need_a_checkpointer() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    runner = AgentLoopRunner(deps, settings=deps.settings)

    assert runner.checkpointing_enabled is False
    with pytest.raises(CheckpointingDisabledError, match="checkpointing=True"):
        await runner.state_history()
    with pytest.raises(CheckpointingDisabledError):
        await runner.resume_from("nope")
