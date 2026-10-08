"""Autonomous delegation regressions (plan 3.5.5, E ownership).

Production paths only (no network/clock). The stand-in model answers the
new minimal contracts: purpose+steps path proposals and worker_next_action
verdicts; completion conditions use the typed-claim shapes.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import AgentId, ApprovalId
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.analysis import BudgetedAnalysisVerifier, DeterministicAnalysisVerifier
from redteam.orchestration.analysis_budget import verification_guard
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.cognition import DecisionBudget, ReasoningHalted
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.delegation import DelegationRequest
from redteam.orchestration.delegation_decision import open_delegation
from redteam.orchestration.dispatch import WorkerOutcome
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.nodes import aggregate_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.observations import default_result_mapper_registry
from redteam.orchestration.reasoning_time import reasoning_time_allowed
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason, initial_graph_state
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes import DummyToolGateway, FakeControlPlane
from tests.integration import _engine_llm_harness as h

RUN_TWO = "run-del-two"


class MinimalLLM:
    """Stage-queued stand-in speaking the new minimal contracts."""

    def __init__(self) -> None:
        self._queues: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, stage: str, response: Any) -> None:
        self._queues.setdefault(stage, []).append(response)

    @staticmethod
    def _stage(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "actions" in props and "decision" in props:
            return "worker_next_action"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        if "criteria" in props:
            return "goal_completion_review"
        return "unknown"

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        num_retries: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        stage = self._stage(response_schema)
        self.calls.append({"stage": stage, "messages": messages})
        queue = self._queues.get(stage, [])
        if not queue:
            raise AssertionError(f"no scripted response left for stage {stage!r}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(self, **_: Any) -> str:
        return ""


def _real_deps(llm, *, seed, world):
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()])
    control = FakeControlPlane()
    deps = RuntimeDeps(
        control_plane=control,
        tool_gateway=gateway,
        event_bus=world.deps.event_bus,
        graph_store=seed.store,
        evidence_store=world.evidence,
        llm_client=llm,
        verifier=DeterministicAnalysisVerifier(),
        engagement=seed.engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        result_mappers=default_result_mapper_registry(),
        work_store=world.work,
        task_graph_store=world.tasks,
        path_store=world.paths,
        settings=OrchestrationSettings(decision_max_attempts=5),
    )
    return deps, control


def minimal_draft(node_id: str) -> dict[str, Any]:
    """Purpose+steps only: no path/run attribution, typed-claim criteria."""
    return {
        "reason": "map the in-scope host inventory",
        "steps": [
            {
                "step_id": "step-1",
                "objective": "confirm the host answers inventory",
                "agent_id": h.RECON_AGENT,
                "target_node_ids": [node_id],
                "completion_criteria": [
                    {
                        "kind": "evidence",
                        "description": "inventory result stored for the host",
                        "min_evidence": 1,
                    }
                ],
            }
        ],
    }


def delegate_turn() -> dict[str, Any]:
    return {
        "mode": "delegate",
        "reason": "let the recon specialist choose methods",
        "delegations": [
            {
                "path_step_id": "step-1",
                "agent_id": h.RECON_AGENT,
                "objective": "map the host inventory",
                "budget": {"max_actions": 4},
            }
        ],
    }


def worker_propose(node_id: str, probe: str) -> dict[str, Any]:
    return {
        "decision": "propose_actions",
        "actions": [
            {
                "target_node_id": node_id,
                "tool_id": h.INVENTORY_TOOL,
                "arguments": {"probe": probe},
                "reason": f"inventory sweep {probe}",
            }
        ],
        "summary": f"worker sweep {probe}",
    }


def root_propose(node_id: str) -> dict[str, Any]:
    response = h.turn_actions(node_id)
    for action in response["actions"]:
        action.pop("capability", None)
        action.pop("operation", None)
    return response


async def _delegated_setup(run_id: str):
    llm = MinimalLLM()
    seed = await h.seed_host(run_id)
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=5)
    deps, control = _real_deps(llm, seed=seed, world=world)
    runtime = h.make_runtime(run_id, "eng-t1")
    engine = InvestigationEngine(deps)
    llm.add("path_draft", minimal_draft(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", delegate_turn())
    path, delegated, note = await engine.decide(runtime, GraphObservation())
    assert note == "continue_planning"
    runtime = engine.updated_runtime
    assert len(runtime.delegations) == 1
    delegation_id = next(iter(runtime.delegations))
    assert runtime.delegations[delegation_id].status == "active"
    return SimpleNamespace(
        llm=llm,
        seed=seed,
        world=world,
        deps=deps,
        control=control,
        runtime=runtime,
        engine=engine,
        path=path,
        delegation_id=delegation_id,
    )


async def test_minimal_purpose_steps_draft_registers() -> None:
    llm = MinimalLLM()
    seed = await h.seed_host("run-del-minimal")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=5)
    deps, _ = _real_deps(llm, seed=seed, world=world)
    engine = InvestigationEngine(deps)
    draft = minimal_draft(seed.node_id)
    assert set(draft) == {"reason", "steps"}
    llm.add("path_draft", draft)
    llm.add("path_review", h.review_accept())
    llm.add("next_action", root_propose(seed.node_id))
    path, compiled, note = await engine.decide(
        h.make_runtime("run-del-minimal", "eng-t1"), GraphObservation()
    )
    assert note is None
    assert path is not None
    assert len(compiled) == 1
    assert str(compiled[0].tool.tool_id) == h.INVENTORY_TOOL


async def test_worker_two_followups_share_control_ledger_and_collection() -> None:
    ctx = await _delegated_setup(RUN_TWO)
    runtime = ctx.runtime
    proposals = []
    real_evidence_ids = []
    for turn, probe in enumerate(("one", "two"), start=1):
        ctx.llm.add("worker_next_action", worker_propose(ctx.seed.node_id, probe))
        planned = await plan_node(
            {**initial_graph_state(runtime), "observation": GraphObservation()},
            deps=ctx.deps,
        )
        runtime = planned["runtime"]
        dispatch = planned["dispatch_items"]
        assert len(dispatch) == 1
        item = dispatch[0]
        assert item.plan_step.delegation_id == ctx.delegation_id
        rows = [
            row
            for row in await ctx.world.work.list_items(run_id=runtime.run_id)
            if row.delegation_id == ctx.delegation_id
        ]
        assert len(rows) == turn
        assert len({row.candidate_key for row in rows}) == turn
        worker_result = await worker_node(
            {"runtime": runtime, "dispatch_item": item},
            deps=ctx.deps,
        )
        outcome = worker_result["worker_outcomes"][str(item.proposal.action_id)]
        assert outcome.error is None
        assert outcome.executed is True
        assert outcome.succeeded is True
        assert outcome.policy_verdict is PolicyVerdict.ALLOW
        assert outcome.model_decision_attempts == 0
        assert outcome.evidence_ids != []
        real_evidence_ids.extend(outcome.evidence_ids)
        proposals.append(item.proposal)
        aggregated = await aggregate_node(
            {**planned, "worker_outcomes": worker_result["worker_outcomes"]},
            deps=ctx.deps,
        )
        runtime = aggregated["runtime"]
        tool_records = [
            record
            for record in await ctx.world.evidence.list_for_run(RUN_TWO)
            if record.source_type is EvidenceSourceType.TOOL
        ]
        assert len(tool_records) == turn
    assert isinstance(ctx.deps.verifier, BudgetedAnalysisVerifier)
    gateway = ctx.deps.tool_gateway
    assert len(gateway.invocations) == 2
    assert [invocation.arguments.get("probe") for invocation in gateway.invocations] == [
        "one",
        "two",
    ]
    assert len({invocation.invocation_id for invocation in gateway.invocations}) == 2
    assert len(ctx.control.evaluated) == 2
    assert [str(entry.action_id) for entry in ctx.control.evaluated] == [
        str(proposal.action_id) for proposal in proposals
    ]
    by_action = {str(record.metadata["action_id"]): record for record in tool_records}
    assert set(by_action) == {str(proposal.action_id) for proposal in proposals}
    for invocation in gateway.invocations:
        record = by_action[str(invocation.action_id)]
        assert str(record.metadata["tool_result"]["invocation_id"]) == str(invocation.invocation_id)
        assert str(record.invocation_id) == str(invocation.invocation_id)
    assert set(real_evidence_ids) == {str(record.evidence_id) for record in tool_records}
    for record in tool_records:
        assert record.metadata.get("plan_step", {}).get("delegation_id") == ctx.delegation_id
    rows = [
        row
        for row in await ctx.world.work.list_items(run_id=runtime.run_id)
        if row.delegation_id == ctx.delegation_id
    ]
    assert len(rows) == 2
    assert {row.status for row in rows} == {WorkStatus.SUCCEEDED}
    control_obs = [
        obs
        for obs in runtime.recent_observations
        if obs.kind == "control" and obs.outcome == PolicyVerdict.ALLOW.value
    ]
    assert len(control_obs) >= 2
    assert runtime.actions_executed == 2
    delegation = runtime.delegations[ctx.delegation_id]
    assert delegation.status == "active"
    assert delegation.actions_used == 2
    assert delegation.decision_attempts_used == 2
    assert delegation.evidence_ids == real_evidence_ids
    assert len(real_evidence_ids) == 2
    assert delegation.tokens_used is None
    assert runtime.pending_approval_id is None


async def test_resolved_allow_deny_clear_pending() -> None:
    from tests.fakes import build_test_application
    from tests.unit.test_fanout_approval_accounting import dispatch_item as fanout_item
    from tests.unit.test_fanout_approval_accounting import state_with

    deps = build_test_application()
    item = fanout_item("action_appr", "a.local")
    base_rt = RuntimeState(run_id=item.proposal.run_id, engagement_id=item.proposal.engagement_id)
    pending_outcome = WorkerOutcome(
        action_id="action_appr",
        work_id=item.work_id,
        policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
        approval_id=ApprovalId("appr_action_appr"),
    )
    held = await aggregate_node(
        state_with(
            base_rt,
            dispatch_items=[item],
            worker_outcomes={"action_appr": pending_outcome},
        ),
        deps=deps,
    )
    assert held["runtime"].pending_approval_id == ApprovalId("appr_action_appr")
    assert held["runtime"].termination_reason == TerminationReason.APPROVAL_PENDING

    for verdict, executed in ((PolicyVerdict.ALLOW, True), (PolicyVerdict.DENY, False)):
        resolved = WorkerOutcome(
            action_id="action_appr",
            work_id=item.work_id,
            executed=executed,
            succeeded=True if executed else None,
            policy_verdict=verdict,
            approval_id=ApprovalId("appr_action_appr"),
        )
        cleared = await aggregate_node(
            state_with(
                held["runtime"],
                dispatch_items=[item],
                worker_outcomes={"action_appr": resolved},
            ),
            deps=deps,
        )
        assert cleared["runtime"].pending_approval_id is None
        assert cleared["runtime"].termination_reason != TerminationReason.APPROVAL_PENDING


async def test_parked_pending_does_not_block_worker_while_leased_does() -> None:
    ctx = await _delegated_setup("run-del-parked")
    now = datetime.now(timezone.utc)
    await ctx.world.work.add(
        WorkItem(
            work_id="work-parked",
            run_id=ctx.runtime.run_id,
            candidate_key="candidate-parked",
            agent_id=AgentId(h.RECON_AGENT),
            target_node_ids=[ctx.seed.node_id],
            priority=PriorityScore(total=1),
            status=WorkStatus.PENDING,
            delegation_id=ctx.delegation_id,
            created_at=now,
            updated_at=now,
        )
    )
    ctx.llm.add("worker_next_action", worker_propose(ctx.seed.node_id, "parked"))
    _, candidates, _ = await ctx.engine.decide(ctx.runtime, GraphObservation())
    assert len(candidates) == 1
    assert "worker_next_action" in [call["stage"] for call in ctx.llm.calls]

    llm2 = MinimalLLM()
    seed2 = await h.seed_host("run-del-leased")
    world2 = h.make_world(llm2, graph_store=seed2.store, decision_max_attempts=5)
    deps2, _ = _real_deps(llm2, seed=seed2, world=world2)
    engine2 = InvestigationEngine(deps2)
    runtime2 = h.make_runtime("run-del-leased", "eng-t1")
    llm2.add("path_draft", minimal_draft(seed2.node_id))
    llm2.add("path_review", h.review_accept())
    llm2.add("next_action", delegate_turn())
    await engine2.decide(runtime2, GraphObservation())
    runtime2 = engine2.updated_runtime
    leased_id = next(iter(runtime2.delegations))
    await world2.work.add(
        WorkItem(
            work_id="work-leased",
            run_id=runtime2.run_id,
            candidate_key="candidate-leased",
            agent_id=AgentId(h.RECON_AGENT),
            target_node_ids=[seed2.node_id],
            priority=PriorityScore(total=1),
            status=WorkStatus.LEASED,
            delegation_id=leased_id,
            created_at=now,
            updated_at=now,
        )
    )
    llm2.add("next_action", root_propose(seed2.node_id))
    _, leased_candidates, leased_note = await engine2.decide(runtime2, GraphObservation())
    assert "worker_next_action" not in [call["stage"] for call in llm2.calls]
    assert leased_note in (None, "continue_planning")
    assert all(
        getattr(candidate.plan_step, "delegation_id", None) is None
        for candidate in leased_candidates
    )


async def test_analysis_retry_time_guard_and_unknown_usage_accumulate() -> None:
    budget = DecisionBudget(5)
    budget.add_usage(None)
    budget.add_usage(None)
    assert budget.unknown_usage_count == 2
    assert budget.tokens_used == 0
    assert budget.tokens_fully_known is False
    assert budget.token_halt_required(100) is True

    llm = MinimalLLM()
    seed = await h.seed_host("run-del-analysis")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=5)
    deps, _ = _real_deps(llm, seed=seed, world=world)
    stopped = h.make_runtime("run-del-analysis", "eng-t1").model_copy(
        update={
            "is_terminal": True,
            "termination_reason": TerminationReason.USER_INTERRUPTED,
        }
    )
    guard = verification_guard({"runtime": stopped}, deps=deps, budget=DecisionBudget(3))
    with pytest.raises(ReasoningHalted):
        await guard()

    expired_engagement = seed.engagement.model_copy(
        update={
            "execution_budget": seed.engagement.execution_budget.model_copy(
                update={"max_duration_seconds": 1}
            )
        }
    )
    ancient = h.make_runtime("run-del-analysis", "eng-t1").model_copy(
        update={"started_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    )
    assert reasoning_time_allowed(expired_engagement, ancient) is False
    expired_deps = dataclasses.replace(deps, engagement=expired_engagement)
    expired_guard = verification_guard(
        {"runtime": ancient}, deps=expired_deps, budget=DecisionBudget(3)
    )
    with pytest.raises(ReasoningHalted, match="analysis_engagement_time_exhausted"):
        await expired_guard()


async def test_opened_delegation_never_reissues_initial_budget() -> None:
    first = open_delegation(
        DelegationRequest(path_step_id="step-1", agent_id=h.RECON_AGENT, objective="map inventory"),
        path_id="path-1",
        path_version=1,
        decision_limit=4,
    )
    assert first.budget.max_actions >= 1
    assert first.decision_attempts_used == 0
    reopened = open_delegation(
        DelegationRequest(path_step_id="step-1", agent_id=h.RECON_AGENT, objective="map inventory"),
        path_id="path-1",
        path_version=1,
        decision_limit=4,
    )
    assert reopened.delegation_id != first.delegation_id


async def test_worker_provider_failure_preserves_diagnostic_usage_and_report(tmp_path):
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.attack_graph.service import AttackGraphService
    from redteam.core.enums import AgentPhase
    from redteam.core.ports.llm import LLMProviderFailure
    from redteam.orchestration.nodes.plan import plan_node
    from redteam.orchestration.nodes.replan import replan_node
    from redteam.orchestration.reporting import ReportingAgent
    from redteam.orchestration.state import initial_graph_state

    ctx = await _delegated_setup("run-worker-provider")
    ctx.llm.add("worker_next_action", LLMProviderFailure(
        kind="auth", http_status=401, provider_code="invalid_api_key", retryable=False,
    ))
    calls_before = len(ctx.llm.calls)
    planned = await plan_node(initial_graph_state(ctx.runtime), deps=ctx.deps)
    runtime = (await replan_node(planned, deps=ctx.deps))["runtime"]
    assert runtime.phase == AgentPhase.FINISHED and runtime.planning_error is None
    assert runtime.termination_reason == "provider_unavailable"
    assert runtime.planning_diagnostic.provider_code == "invalid_api_key"
    delegation = runtime.delegations[ctx.delegation_id]
    assert len(ctx.llm.calls) == calls_before + 1
    assert delegation.status == "finished"
    assert delegation.termination_reason == "provider_failure"
    assert delegation.decision_attempts_used == 1
    assert runtime.model_requests_used == 4 and runtime.model_usage_unknown == 4
    assert ctx.deps.tool_gateway.invocations == []
    assert len(runtime.unresolved_evidence_ids) == 1
    record = await ctx.world.evidence.get(runtime.unresolved_evidence_ids[0])
    assert record.metadata["claim_supported"] is False
    assert record.metadata["diagnostic"]["provider_status"] == 401
    assert record.metadata["gaps"][0]["code"] == "provider_failure"
    runtime = RuntimeState.model_validate_json(runtime.model_dump_json())
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(ctx.seed.store),
        evidence_store=ctx.world.evidence, work_store=ctx.world.work,
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored.run_outcome == "unresolved" and not restored.ready_for_release
    assert restored.unresolved_details[0].diagnostic.provider_code == "invalid_api_key"
    assert "status=401" in _render_markdown(restored)
