"""The re-plan loop end to end: multi-turn, multi-agent, and replayable.

Complements ``test_end_to_end.py`` (one happy-path turn) with what the
loop itself adds: iteration, agent hand-over, named stop conditions, the
ALLOW gate, and checkpoint rewind.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import cast

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.predicates import attrs_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import (
    AgentPhase,
    GraphNodeType,
    PolicyVerdict,
    ToolHealthStatus,
    Verdict,
)
from redteam.core.ids import (
    AgentId,
    ApprovalId,
    EngagementId,
    EventId,
    GraphNodeId,
    RunId,
    ToolId,
    new_id,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import (
    AgentDefinition,
    AgentRegistry,
    AgentSpec,
    CandidateProviderRegistry,
    ContextProfile,
    PlanStep,
)
from redteam.orchestration.agents.registry import RECON_AGENT
from redteam.orchestration.analysis import VerificationAssessment
from redteam.orchestration.candidates.models import CandidateDraft, PriorityScore
from redteam.orchestration.events import REPORT_PROJECTED
from redteam.orchestration.graph import build_graph
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason, initial_graph_state
from redteam.orchestration.work.models import WorkItem, WorkStatus
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


async def run_with(
    deps, target: Engagement, *, run_id: RunId | None = None
) -> RuntimeState:
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    return await runner.run(
        RuntimeState(
            run_id=run_id or RunId(new_id("run")),
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

    work = await deps.work_store.list_items()
    assert len(work) == 3
    assert all(item.status is WorkStatus.SUCCEEDED for item in work)
    projected = bus(deps).history(event_type=REPORT_PROJECTED)
    assert len(projected) == 1
    assert projected[0].payload["ready_for_release"] is True
    assert projected[0].payload["evidence_count"] == 3


async def test_a_denied_host_is_never_seeded_and_never_probed(tmp_path) -> None:
    target = engagement("a.local", "b.local", denied=["b.local"])
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)

    final = await run_engagement(target, deps=deps)

    assert final.actions_executed == 1
    service = AttackGraphService(deps.graph_store)
    assert {str(n.id) for n in await service.query(node_type=GraphNodeType.HOST)} == {
        "host:a.local"
    }


async def test_pending_approval_has_its_own_terminal_reason_and_partial_report() -> None:
    class ApprovalControl:
        async def evaluate(
            self, proposal, context: ControlContext
        ) -> PolicyDecision:
            del context
            return PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=["human approval required"],
                approval_id=ApprovalId("appr_test"),
                evaluated_at=datetime.now(timezone.utc),
            )

        async def approval_outcome(self, approval_id):
            del approval_id
            return None

    target = engagement("a.local")
    gateway = DummyToolGateway()
    deps = build_test_application(control_plane=ApprovalControl(), tool_gateway=gateway)

    final = await run_engagement(target, deps=deps)

    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.APPROVAL_PENDING
    assert final.pending_approval_id == ApprovalId("appr_test")
    assert gateway.invocations == []
    projected = bus(deps).history(event_type=REPORT_PROJECTED)
    assert projected[0].payload["ready_for_release"] is False
    assert projected[0].payload["pending_approval_id"] == "appr_test"


async def test_runner_request_stop_returns_a_named_terminal_runtime() -> None:
    class BlockingGateway:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def invoke(self, invocation):
            del invocation
            self.started.set()
            await asyncio.Event().wait()

    target = engagement("a.local")
    gateway = BlockingGateway()
    deps = build_test_application(tool_gateway=gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    task = asyncio.create_task(
        runner.run(
            RuntimeState(
                run_id=RunId("run_stop"),
                engagement_id=target.engagement_id,
                remaining_budget=target.execution_budget.max_actions,
            )
        )
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=1)

    assert runner.request_stop() is True
    final = await asyncio.wait_for(task, timeout=1)

    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.USER_INTERRUPTED
    assert all(
        item.status is WorkStatus.FAILED
        for item in await deps.work_store.list_items(run_id=RunId("run_stop"))
    )


async def test_unexpected_run_failure_still_publishes_a_partial_report(
    monkeypatch,
) -> None:
    async def fail_run(self, runtime):
        del self, runtime
        raise RuntimeError("sensitive failure detail")

    monkeypatch.setattr(AgentLoopRunner, "run", fail_run)
    target = engagement("a.local")
    deps = build_test_application()

    with pytest.raises(RuntimeError, match="sensitive failure detail"):
        await run_engagement(target, deps=deps)

    projected = bus(deps).history(event_type=REPORT_PROJECTED)
    finished = bus(deps).history(event_type=EventType.RUN_FINISHED)
    assert len(projected) == 1
    assert projected[0].payload["ready_for_release"] is False
    assert finished[-1].payload["termination_reason"] == TerminationReason.EXECUTION_ERROR
    assert finished[-1].payload["error_type"] == "RuntimeError"


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


async def test_an_injected_analysis_verifier_owns_the_security_verdict() -> None:
    class IndependentVerifier:
        async def verify(self, request):
            return VerificationAssessment(
                verdict=Verdict.VIOLATION,
                reason="independent evidence review confirmed the condition",
                reviewer_id="analysis:test-reviewer",
                source_refs=request.source_refs,
            )

    deps = build_test_application(
        verifier=IndependentVerifier(),
        settings=OrchestrationSettings(checkpointing=False),
    )
    await run_with(deps, engagement("a.local"))

    findings = await AttackGraphService(deps.graph_store).list_findings(verified=True)
    assert len(findings) == 1
    assert findings[0].metadata["reviewer_id"] == "analysis:test-reviewer"


async def test_verifier_exception_preserves_evidence_without_asserting_predicates() -> None:
    class ExplodingVerifier:
        async def verify(self, request):
            del request
            raise RuntimeError("sensitive verifier failure")

    deps = build_test_application(
        verifier=ExplodingVerifier(),
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))
    service = AttackGraphService(deps.graph_store)
    host = (await service.query(node_type=GraphNodeType.HOST))[0]
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    work = await deps.work_store.list_items(run_id=final.run_id)

    assert final.last_verdict is Verdict.UNKNOWN
    assert attrs_of(host.properties).get("reachable") is None
    assert len(evidence) == 1
    assert work[0].status is WorkStatus.FAILED
    assert work[0].outcome_reason == "verification_exception:RuntimeError"
    assert (await service.integrity_report()).blocking == []


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


async def test_a_repeatable_plan_that_stops_changing_the_graph_is_stopped() -> None:
    repeating = RECON_AGENT.model_copy(
        deep=True,
        update={
            "agent_id": AgentId("repeating-agent"),
            "playbook": [RECON_AGENT.playbook[0].model_copy(update={"repeatable": True})],
        },
    )
    deps = build_test_application(
        agents=AgentRegistry([repeating]),
        settings=OrchestrationSettings(
            max_steps=20,
            max_no_progress_turns=2,
            checkpointing=False,
        ),
    )

    final = await run_with(deps, engagement("a.local", max_actions=20))

    assert final.termination_reason == TerminationReason.STALLED
    assert final.actions_executed < 20


async def test_per_agent_action_budget_caps_a_parallel_batch() -> None:
    capped = RECON_AGENT.model_copy(
        deep=True,
        update={"agent_id": AgentId("capped-agent"), "max_actions": 2},
    )
    deps = build_test_application(
        agents=AgentRegistry([capped]),
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=5,
            max_actions_per_turn=5,
        ),
    )

    final = await run_with(
        deps,
        engagement(*[f"budget-{index}.local" for index in range(5)], max_actions=10),
    )

    assert final.actions_executed == 2
    assert final.actions_by_agent == {"capped-agent": 2}


# -- the Control Plane gate ----------------------------------------------


@pytest.mark.parametrize("verdict", [PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL])
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
    # A policy-blocked proposal was never tested. Its pre-recorded hypothesis
    # must be closed as inconclusive instead of poisoning terminal reporting.
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_control_plane_exception_fails_closed_and_releases_the_lease() -> None:
    class ExplodingControlPlane:
        async def evaluate(self, proposal, context):
            del proposal, context
            raise RuntimeError("sensitive policy backend detail")

        async def approval_outcome(self, approval_id):
            del approval_id
            return None

    gateway = DummyToolGateway()
    deps = build_test_application(
        control_plane=ExplodingControlPlane(),
        tool_gateway=gateway,
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))
    work = await deps.work_store.list_items(run_id=final.run_id)

    assert gateway.invocations == []
    assert final.actions_executed == 0
    assert work[0].status is WorkStatus.BLOCKED
    assert work[0].outcome_reason == PolicyVerdict.DENY.value
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_a_denied_action_still_retires_the_agent_rather_than_spinning() -> None:
    # What ends the run is the agent running out of steps, not a retry loop.
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
                capability="recon",
                operation="endpoint_discovery",
                tool_id=ToolId("recon.ping"),
                target_bind="host",
                preconditions=[
                    # Only offered for hosts the recon agent already
                    # proved reachable — one agent's output is the next
                    # one's frontier.
                    RECON_AGENT.playbook[0]
                    .preconditions[0]
                    .model_copy(deep=True, update={"attrs": {"reachable": True}})
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
    started = [e.payload["agent_id"] for e in bus(deps).history(event_type=EventType.AGENT_STARTED)]
    assert started == ["recon-agent", "follow-up-agent"]


async def test_default_web_agent_activates_when_target_and_catalog_tool_exist() -> None:
    registry = MCPToolRegistry()
    for tool_id, capability in (
        ("recon.ping", "recon"),
        ("web.http_inspect", "web"),
    ):
        registry.register(
            ToolDescriptor(
                tool_id=ToolId(tool_id),
                server_id="test",
                tool_name=tool_id,
                capability_tags=[capability],
                health_status=ToolHealthStatus.HEALTHY,
            )
        )
    catalog = MCPToolGateway(
        registry, {}, {"recon.ping": ["service_discovery"], "web.http_inspect": ["http_request"]}
    )
    deps = build_test_application(
        tool_catalog=catalog,
        settings=OrchestrationSettings(max_steps=10, checkpointing=False),
    )
    await AttackGraphService(deps.graph_store).upsert_node(
        GraphNodeType.ENDPOINT,
        key="a.local GET /",
    )

    final = await run_with(deps, engagement("a.local"))

    assert final.actions_executed == 2
    assert AgentId("web-api-agent") in final.finished_agents


async def test_round_robin_prevents_recon_from_consuming_all_specialist_budget() -> None:
    registry = MCPToolRegistry()
    for tool_id, capability in (
        ("recon.ping", "recon"),
        ("web.http_inspect", "web"),
    ):
        registry.register(
            ToolDescriptor(
                tool_id=ToolId(tool_id),
                server_id="test",
                tool_name=tool_id,
                capability_tags=[capability],
                health_status=ToolHealthStatus.HEALTHY,
            )
        )
    catalog = MCPToolGateway(
        registry, {}, {"recon.ping": ["service_discovery"], "web.http_inspect": ["http_request"]}
    )
    gateway = DummyToolGateway()
    deps = build_test_application(
        tool_catalog=catalog,
        tool_gateway=gateway,
        settings=OrchestrationSettings(max_steps=10, checkpointing=False),
    )
    await AttackGraphService(deps.graph_store).upsert_node(
        GraphNodeType.ENDPOINT,
        key="a.local GET /",
    )

    final = await run_with(
        deps,
        engagement("a.local", "b.local", "c.local", max_actions=2),
    )

    assert final.actions_executed == 2
    assert {item.operation for item in gateway.invocations} == {
        "service_discovery",
        "http_request",
    }


async def test_custom_provider_adds_an_agent_without_editing_the_central_graph() -> None:
    agent_id = AgentId("plugin-agent")
    plugin = AgentSpec(agent_id=agent_id, name="Plugin Agent", playbook=[])

    class Provider:
        async def propose(self, spec, view, observation, *, step):
            del view, observation, step
            plan_step = PlanStep(
                step_id="plugin-probe",
                capability="recon",
                operation="service_discovery",
                tool_id=ToolId("recon.ping"),
                target=HostTarget(host="a.local"),
                reason="candidate came from a registered provider",
            )
            return [
                CandidateDraft(
                    agent_id=spec.agent_id,
                    plan_step=plan_step,
                    target_node_ids=[GraphNodeId("host:a.local")],
                    conflict_keys=["target:host:a.local"],
                )
            ]

    providers = CandidateProviderRegistry([("plugin:test", Provider())])
    agents = AgentRegistry(
        [plugin],
        definitions=[
            AgentDefinition(
                agent_id=agent_id,
                role="plugin",
                name="Plugin Agent",
                provider_id="plugin:test",
                context_profile=ContextProfile(profile_id="plugin"),
            )
        ],
    )
    deps = build_test_application(
        agents=agents,
        candidate_providers=providers,
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))

    assert final.actions_executed == 1
    assert final.finished_agents == [agent_id]


async def test_broken_provider_is_retired_without_aborting_other_agents() -> None:
    broken_id = AgentId("broken-agent")

    class BrokenProvider:
        async def propose(self, spec, view, observation, *, step):
            del spec, view, observation, step
            raise RuntimeError("sensitive plugin detail")

    providers = CandidateProviderRegistry([("plugin:broken", BrokenProvider())])
    agents = AgentRegistry(
        [AgentSpec(agent_id=broken_id, name="Broken"), RECON_AGENT],
        definitions=[
            AgentDefinition(
                agent_id=broken_id,
                role="broken",
                name="Broken",
                provider_id="plugin:broken",
                priority_band=1,
                context_profile=ContextProfile(profile_id="broken"),
            )
        ],
    )
    deps = build_test_application(
        agents=agents,
        candidate_providers=providers,
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local"))

    assert final.actions_executed == 1
    assert broken_id in final.finished_agents
    assert "AgentProviderFailed" in bus(deps).event_types()


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

    # Rewinding rewinds GraphState only — the graph kept the attempts, so
    # the replayed branch finds a smaller frontier. Evidence is append-only.
    assert resumed.phase is AgentPhase.FINISHED
    assert resumed.actions_executed >= earlier.actions_executed


async def test_one_runner_refuses_to_mix_two_run_ids() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    await runner.run(
        RuntimeState(run_id=RunId("run-one"), engagement_id=target.engagement_id)
    )

    with pytest.raises(ValueError, match="only one run"):
        await runner.run(
            RuntimeState(run_id=RunId("run-two"), engagement_id=target.engagement_id)
        )


async def test_parallel_dispatch_state_is_checkpointable() -> None:
    deps = build_test_application(
        settings=OrchestrationSettings(
            checkpointing=True,
            global_concurrency=2,
            max_actions_per_turn=2,
        )
    )
    target = engagement("a.local", "b.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)

    final = await runner.run(
        RuntimeState(
            run_id=RunId("run_parallel_checkpoint"),
            engagement_id=target.engagement_id,
            remaining_budget=2,
        )
    )
    history = await runner.state_history()

    assert final.actions_executed == 2
    assert history
    assert any(snapshot.actions_executed == 2 for snapshot in history)


async def test_parallel_worker_outcomes_are_cleared_after_each_batch() -> None:
    settings = OrchestrationSettings(
        checkpointing=False,
        max_steps=10,
        global_concurrency=2,
        max_actions_per_turn=2,
    )
    deps = build_test_application(settings=settings)
    target = engagement("a.local", "b.local", "c.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    graph = build_graph(deps)

    final = await graph.ainvoke(
        initial_graph_state(
            RuntimeState(
                run_id=RunId("run_transient_worker_outcomes"),
                engagement_id=target.engagement_id,
                remaining_budget=3,
            )
        ),
        config={"recursion_limit": settings.effective_recursion_limit},
    )

    assert final["runtime"].actions_executed == 3
    assert final["worker_outcomes"] == {}


async def test_active_ledger_work_is_not_misreported_as_convergence() -> None:
    deps = build_test_application(
        agents=AgentRegistry([]),
        settings=OrchestrationSettings(checkpointing=False),
    )
    now = datetime.now(timezone.utc)
    run_id = RunId("run_orphaned")
    await deps.work_store.add(
        WorkItem(
            work_id="work_orphaned",
            run_id=run_id,
            candidate_key="candidate_orphaned",
            agent_id=AgentId("missing-agent"),
            target_node_ids=[GraphNodeId("host:a.local")],
            priority=PriorityScore(total=1.0),
            created_at=now,
            updated_at=now,
        )
    )

    final = await run_with(deps, engagement("a.local"), run_id=run_id)

    assert final.phase is AgentPhase.FAILED
    assert final.termination_reason == TerminationReason.INCOMPLETE_ACTIVE_WORK


async def test_event_reconciliation_reactivates_agent_for_a_new_graph_target() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    runtime = await runner.run(
        RuntimeState(
            run_id=RunId("run_reconcile"),
            engagement_id=target.engagement_id,
            remaining_budget=3,
        )
    )
    assert runtime.actions_executed == 1

    service = AttackGraphService(deps.graph_store)
    await service.upsert_node(
        GraphNodeType.HOST,
        key="b.local",
        attrs={"in_scope": True},
        step=runtime.step_count + 1,
    )
    pending = asyncio.create_task(runner.reconcile_once(runtime, timeout_seconds=1.0))
    await asyncio.sleep(0)
    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId("evt_new_target"),
            event_type=EventType.GRAPH_UPDATED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="test",
            timestamp=datetime.now(timezone.utc),
            payload={"node_id": "host:b.local"},
        )
    )
    reconciled = await pending

    assert reconciled.resumed is True
    assert reconciled.wakeup.triggered is True
    assert reconciled.runtime.actions_executed == 2


async def test_periodic_reconciliation_stays_idle_without_canonical_change() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    runtime = await runner.run(
        RuntimeState(
            run_id=RunId("run_idle_reconcile"),
            engagement_id=target.engagement_id,
            remaining_budget=3,
        )
    )

    reconciled = await runner.reconcile_once(runtime, timeout_seconds=0.01)

    assert reconciled.wakeup.reason == "periodic_reconciliation"
    assert reconciled.resumed is False
    assert reconciled.runtime.step_count == runtime.step_count
    assert reconciled.runtime.termination_reason == runtime.termination_reason


async def test_sqlite_runner_history_survives_a_new_runner(tmp_path) -> None:
    settings = OrchestrationSettings(
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp_path / "runner.sqlite3"),
        thread_id="run_sqlite",
    )
    deps = build_test_application(settings=settings)
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)

    first = AgentLoopRunner(deps, settings=settings)
    await first.run(
        RuntimeState(
            run_id=RunId("run_sqlite"),
            engagement_id=target.engagement_id,
            remaining_budget=2,
        )
    )
    await first.aclose()

    reopened = AgentLoopRunner(deps, settings=settings)
    history = await reopened.state_history()
    await reopened.aclose()

    assert history
    assert history[0].phase is AgentPhase.FINISHED


async def test_sqlite_runner_reopens_its_saver_after_close(tmp_path) -> None:
    settings = OrchestrationSettings(
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp_path / "reusable-runner.sqlite3"),
        thread_id="run_reusable_sqlite",
    )
    deps = build_test_application(settings=settings)
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=settings)
    await runner.run(
        RuntimeState(
            run_id=RunId("run_reusable_sqlite"),
            engagement_id=target.engagement_id,
            remaining_budget=1,
        )
    )
    await runner.aclose()

    history = await runner.state_history()
    await runner.aclose()

    assert history
    assert history[0].phase is AgentPhase.FINISHED


async def test_history_and_replay_need_a_checkpointer() -> None:
    deps = build_test_application(settings=OrchestrationSettings(checkpointing=False))
    runner = AgentLoopRunner(deps, settings=deps.settings)

    assert runner.checkpointing_enabled is False
    with pytest.raises(CheckpointingDisabledError, match="checkpointing=True"):
        await runner.state_history()
    with pytest.raises(CheckpointingDisabledError):
        await runner.resume_from("nope")


# -- report artifacts the graph can produce after a run -------------------


async def test_a_finished_run_produces_a_reportable_graph(tmp_path) -> None:
    target = engagement("a.local", "b.local")
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)

    final = await run_engagement(target, deps=deps)
    service = AttackGraphService(deps.graph_store)

    # Nothing outstanding, so a report may go out.
    report = await service.integrity_report()
    assert report.blocking == []
    assert (await service.cleanup_report()).is_clear

    # The graph renders to a self-contained artifact.
    page = (await service.export_html(tmp_path / "graph.html", title="run")).read_text(
        encoding="utf-8"
    )
    assert "host:a.local".rpartition(":")[2] in page
    assert final.actions_executed == 2


async def test_a_leftover_resource_blocks_the_report(tmp_path) -> None:
    target = engagement("a.local")
    deps = await build_runtime_deps(target, evidence_dir=tmp_path)
    await run_engagement(target, deps=deps)

    service = AttackGraphService(deps.graph_store)
    host = (await service.query(node_type=GraphNodeType.HOST))[0]
    await service.register_created_resource(host.id, resource_type="user_account", step=1)

    assert (await service.integrity_report()).blocking
    await service.mark_cleaned(host.id, evidence_id="ev_cleanup")
    assert (await service.integrity_report()).blocking == []


async def test_stale_predicates_are_swept_as_the_loop_runs() -> None:
    # OBSERVE triggers the sweep each turn; with a tight rule the
    # reachability predicate written on turn 1 is withdrawn later.
    deps = build_test_application(settings=OrchestrationSettings(max_steps=6, checkpointing=False))
    service = AttackGraphService(deps.graph_store)
    await run_with(deps, engagement("a.local"))

    node = (await service.query(node_type=GraphNodeType.HOST))[0]
    assert attrs_of(node.properties)["reachable"] is True

    applied = await service.sweep_stale_predicates(current_step=50)
    assert [item.attr for item in applied] == ["reachable"]


class YieldingGraphStore(InMemoryAttackGraphStore):
    """Awaits between read and write, the way a networked store would — the
    plain in-memory store never yields, so a run against it hides races."""

    async def get_node(self, node_id):
        await asyncio.sleep(0)
        return await super().get_node(node_id)

    async def add_node(self, node) -> None:
        await asyncio.sleep(0)
        await super().add_node(node)


class ConcurrencyTrackingGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def invoke(self, invocation):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.08)
            return await super().invoke(invocation)
        finally:
            self.active -= 1


class ConcurrencyTrackingControlPlane(FakeControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def evaluate(self, proposal, context):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.005)
            return await super().evaluate(proposal, context)
        finally:
            self.active -= 1


class OneShotExplodingGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def invoke(self, invocation):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic transport failure with secret text")
        return await super().invoke(invocation)


class LeaseAwareSlowGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, invocation):
        self.started.set()
        await self.release.wait()
        return await super().invoke(invocation)


async def test_a_full_run_is_correct_against_a_store_that_yields() -> None:
    deps = build_test_application(
        graph_store=YieldingGraphStore(),
        settings=OrchestrationSettings(max_steps=20, checkpointing=False),
    )
    target = engagement(*[f"h{index}.local" for index in range(5)], max_actions=20)

    final = await run_with(deps, target)
    service = AttackGraphService(deps.graph_store)

    assert final.actions_executed == 5
    assert final.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    # Every host got probed exactly once and the graph stays reportable.
    hosts = await service.query(node_type=GraphNodeType.HOST)
    assert len(hosts) == 5
    assert all(attrs_of(node.properties)["reachable"] is True for node in hosts)
    assert (await service.integrity_report()).blocking == []


async def test_bounded_fan_out_executes_independent_targets_concurrently() -> None:
    gateway = ConcurrencyTrackingGateway()
    control = ConcurrencyTrackingControlPlane()
    deps = build_test_application(
        control_plane=control,
        tool_gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=3,
            max_actions_per_turn=3,
        ),
    )

    final = await run_with(
        deps,
        engagement(*[f"parallel-{index}.local" for index in range(5)], max_actions=10),
    )

    assert final.actions_executed == 5
    assert final.remaining_budget == 5
    assert gateway.max_active == 3
    # Admission stays serial because the real Control Plane owns mutable
    # rate-limit and budget counters.
    assert control.max_active == 1
    assert len(gateway.invocations) == 5
    work = await deps.work_store.list_items()
    assert len(work) == 5
    assert all(item.status is WorkStatus.SUCCEEDED for item in work)


async def test_context_profile_bounds_the_candidate_frontier_before_fan_out() -> None:
    bounded_id = AgentId("bounded-frontier-agent")
    bounded = RECON_AGENT.model_copy(
        deep=True,
        update={"agent_id": bounded_id, "planner_kind": "playbook"},
    )
    registry = AgentRegistry(
        [bounded],
        definitions=[
            AgentDefinition(
                agent_id=bounded_id,
                role="bounded",
                name="Bounded",
                provider_id="playbook:recon",
                context_profile=ContextProfile(
                    profile_id="bounded",
                    max_candidates=1,
                ),
                max_concurrency=4,
            )
        ],
    )
    gateway = ConcurrencyTrackingGateway()
    deps = build_test_application(
        agents=registry,
        tool_gateway=gateway,
        settings=OrchestrationSettings(
            checkpointing=False,
            max_steps=10,
            global_concurrency=4,
            max_actions_per_turn=4,
        ),
    )

    final = await run_with(deps, engagement("a.local", "b.local", "c.local"))

    assert final.actions_executed == 3
    assert gateway.max_active == 1
    assert len(await deps.work_store.list_items(run_id=final.run_id)) == 3


async def test_long_running_tool_renews_its_work_lease() -> None:
    gateway = LeaseAwareSlowGateway()
    deps = build_test_application(
        tool_gateway=gateway,
        settings=OrchestrationSettings(
            checkpointing=False,
            lease_ttl_seconds=0.06,
        ),
    )
    task = asyncio.create_task(run_with(deps, engagement("a.local", max_actions=1)))
    await asyncio.wait_for(gateway.started.wait(), timeout=1.0)
    try:
        # Wait past the original lease. The execution heartbeat must keep the
        # row out of reclaim so another scheduler cannot duplicate the action.
        await asyncio.sleep(0.09)
        expired = await deps.work_store.reclaim_expired(now=datetime.now(timezone.utc))
        assert expired == []
    finally:
        gateway.release.set()

    final = await asyncio.wait_for(task, timeout=1.0)
    assert final.actions_executed == 1
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert work[0].status is WorkStatus.SUCCEEDED


async def test_per_target_concurrency_serializes_distinct_actions_on_one_target() -> None:
    first = RECON_AGENT.playbook[0].model_copy(
        deep=True,
        update={"step_id": "first-probe", "operation": "port_scan"},
    )
    second = RECON_AGENT.playbook[0].model_copy(
        deep=True,
        update={"step_id": "second-probe", "operation": "endpoint_discovery"},
    )
    specialist = AgentSpec(
        agent_id=AgentId("same-target-agent"),
        name="Same Target Agent",
        playbook=[first, second],
    )
    gateway = ConcurrencyTrackingGateway()
    deps = build_test_application(
        agents=AgentRegistry([specialist]),
        tool_gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=2,
            max_actions_per_turn=2,
            per_target_concurrency=1,
        ),
    )

    final = await run_with(deps, engagement("a.local", max_actions=3))

    assert final.actions_executed == 2
    assert gateway.max_active == 1


async def test_one_parallel_worker_failure_does_not_cancel_its_siblings() -> None:
    gateway = OneShotExplodingGateway()
    deps = build_test_application(
        tool_gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=2,
            max_actions_per_turn=2,
        ),
    )

    final = await run_with(deps, engagement("a.local", "b.local", max_actions=5))

    assert final.phase is AgentPhase.FINISHED
    assert final.actions_executed == 2
    assert gateway.calls == 2
    work = await deps.work_store.list_items()
    assert any(item.status is WorkStatus.FAILED for item in work)
    assert sum(item.status is WorkStatus.SUCCEEDED for item in work) == 1
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_sequential_gateway_exception_is_normalized_and_closes_work() -> None:
    gateway = OneShotExplodingGateway()
    deps = build_test_application(
        tool_gateway=gateway,
        settings=OrchestrationSettings(checkpointing=False),
    )

    final = await run_with(deps, engagement("a.local", max_actions=2))
    work = await deps.work_store.list_items(run_id=final.run_id)

    assert final.actions_executed == 1
    assert final.phase is AgentPhase.FINISHED
    assert work[0].status is WorkStatus.FAILED
    assert work[0].outcome_reason == "gateway_exception:RuntimeError"
    assert EventType.TOOL_EXECUTION_FAILED in bus(deps).event_types()
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_runner_failure_abandons_its_active_lease() -> None:
    class FailingEventBus(InMemoryEventBus):
        async def publish(self, event):
            if event.event_type == EventType.ACTION_PROPOSED:
                raise RuntimeError("synthetic bus outage")
            await super().publish(event)

    deps = build_test_application(
        event_bus=FailingEventBus(),
        settings=OrchestrationSettings(checkpointing=False),
    )
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    run_id = RunId("run_abandon")
    runner = AgentLoopRunner(deps, settings=deps.settings)

    with pytest.raises(RuntimeError, match="synthetic bus outage"):
        await runner.run(
            RuntimeState(
                run_id=run_id,
                engagement_id=target.engagement_id,
                remaining_budget=2,
            )
        )

    work = await deps.work_store.list_items(run_id=run_id)
    assert work[0].status is WorkStatus.FAILED
    assert work[0].outcome_reason == "runner_error:RuntimeError"
    assert work[0].hypothesis_id is not None
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_runner_abandon_does_not_retract_a_completed_verdict() -> None:
    class FailingGraphEventBus(InMemoryEventBus):
        async def publish(self, event):
            if event.event_type == EventType.GRAPH_UPDATED:
                raise RuntimeError("synthetic graph event outage")
            await super().publish(event)

    class ViolationVerifier:
        async def verify(self, request):
            return VerificationAssessment(
                verdict=Verdict.VIOLATION,
                reason="independently confirmed",
                reviewer_id="analysis:test",
                source_refs=request.source_refs,
            )

    deps = build_test_application(
        event_bus=FailingGraphEventBus(),
        verifier=ViolationVerifier(),
        settings=OrchestrationSettings(checkpointing=False),
    )
    target = engagement("a.local")
    await seed_from_engagement(AttackGraphService(deps.graph_store), target)
    runner = AgentLoopRunner(deps, settings=deps.settings)

    with pytest.raises(RuntimeError, match="synthetic graph event outage"):
        await runner.run(
            RuntimeState(
                run_id=RunId("run_preserve_verdict"),
                engagement_id=target.engagement_id,
            )
        )

    hypotheses = await AttackGraphService(deps.graph_store).list_hypotheses(
        status="confirmed"
    )
    assert len(hypotheses) == 1
