"""Agent loop contracts under the LLM-led engine (scripted model, no network).

Only real security contracts live here; every turn is driven by an explicit
scripted model through the production InvestigationEngine path. Nothing runs
on a rule fallback: the model answers draft/review/action/verify calls and an
unscripted route raises instead of returning an empty decision.

Kept execution contracts, each verified below with an explicit scripted model:
- action budgets: test_execution_budget_stops_the_loop (BUDGET_EXHAUSTED, 2 actions kept).
- role budgets: test_capped_agent_budget_refuses_further_approval (cap reached -> no approval).
- turn ceiling: test_turn_ceiling_stops_the_loop (MAX_STEPS_REACHED, step_count == 2).
- consecutive failures: test_consecutive_failures_stop_hammering (CONSECUTIVE_FAILURES, 2 actions).
- fan-out concurrency: test_bounded_fan_out_executes_concurrently (5 actions, 3-wide overlap).
- per-target serialisation: test_per_target_concurrency_serializes_one_host (distinct
  arguments on one host both run across turns, with at most one action in flight).
- lease renewal: test_long_running_tool_renews_its_work_lease (no reclaim mid-flight, SUCCEEDED).
- worker isolation: test_parallel_worker_failure_isolated (one FAILED, one SUCCEEDED).
- stalls: test_stalled_no_progress_stops_the_loop (STALLED below budget).
- checkpoint history: test_every_turn_checkpointed_and_replayable (history + resume_from)
  and test_sqlite_checkpoint_history_survives (reopen keeps FINISHED).
- approval/policy guards, goal preservation, no auto tasks, failure normalisation:
  the *_blocks_execution_and_closes_lease, *_suspends_with_partial_report,
  *goal_preserved*, *no_automatic_task_creation* and *gateway_exception* tests.

Retired auto-rule assertions only (model intents now decide; no execution contract lost):
- old test_the_loop_probes_every_in_scope_host_exactly_once (rule auto-counts) -> retired;
  single scripted execution -> test_llm_single_probe_executes_original_behavior.
- old test_agents_run_in_order_and_share_one_graph, round-robin, web activation,
  custom/broken providers (rule order + provider auto-selection) -> retired;
  generation seam -> tests/integration/test_llm_candidate_generation_loop.py.
- old per-agent auto-counts beyond the restored cap -> retired;
  the cap itself stays enforced (see role budgets above).
- old denied-host seeding counts, stale sweeps, report rendering counts -> retired;
  unknown-target rejection stays in tests/integration/test_engine_llm_investigation_flow.py.
- old sqlite reopen variants beyond history, reconcile/idle polling, leftover-resource
  rendering -> retired as runner-scaffolding timing tests; lease ownership is pinned by
  test_policy_deny_blocks_execution_and_closes_lease and the lease-renewal test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventType
from redteam.core.contracts.tool import ToolInvocation, ToolResult
from redteam.core.enums import AgentPhase, MissionStatus, PolicyVerdict
from redteam.core.ids import AgentId, EngagementId, RunId
from redteam.core.ports.control_plane import ControlPlane
from redteam.event_bus.memory import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.registry import RECON_AGENT, AgentRegistry, default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import REPORT_PROJECTED
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h

NODE_ID = "host:a.local"


class _LoopLLM:
    """Dynamic scripted model for single-host recon.inventory runs."""

    def __init__(self, evidence: Any, *, finish: bool = True) -> None:
        self._evidence = evidence
        self._finish = finish
        self.calls: list[dict[str, Any]] = []
        self._actions = 0

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    @staticmethod
    def _hold() -> dict[str, Any]:
        return {
            "mode": "hold",
            "reason": "scripted hold",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route})
        if route == "path_draft":
            # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
            full = h.draft_path(NODE_ID)
            return {
                "reason": full.get("reason", "scripted draft"),
                "steps": [
                    {
                        key: value
                        for key, value in step.items()
                        if key not in ("status", "outcome", "evidence_ids")
                    }
                    for step in full.get("steps", [])
                ],
            }
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            # Step review keeps the old accepted/reason shape (no claims).
            return {"accepted": True, "reason": "scripted semantic accept"}
        if route == "task_completion_review":
            # Task review: accepted/reason/claims with actual evidence ids.
            payload = self._payload(messages)
            task_receipts = list(payload["verified_source_ids"])
            assert task_receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "inventory tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(task_receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            # Goal review: every original criterion needs evidence + claims.
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload["verified_source_ids"])
            assert receipts, "goal review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": c,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "inventory tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for c in criteria
                ],
            }
        if route == "next_action":
            self._actions += 1
            if self._actions == 1:
                return h.turn_actions(NODE_ID)
            if not self._finish:
                return self._hold()
            payload = self._payload(messages)
            run_id = payload["active_path"]["run_id"]
            records = await self._evidence.list_for_run(run_id)
            tool_evidence = [
                str(r.evidence_id)
                for r in records
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if self._actions == 2:
                assert tool_evidence, "turn 2 needs the executed call evidence"
                step_id = payload["active_path"]["steps"][0]["step_id"]
                return {**h.turn_complete_step(tool_evidence), "path_step_id": step_id}
            if self._actions == 3:
                # Close the single required task before settling the mission.
                # Empty completion falls back to the completed step evidence.
                return {
                    "mode": "complete_task",
                    "reason": "task evidence ready",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return h.turn_complete_goal()
        raise AssertionError(f"no scripted answer for route {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


class OneShotExplodingGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__(tools=[h.inventory_descriptor()])
        self.calls = 0

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic transport failure")
        return await super().invoke(invocation)


def _engagement(*hosts: str, max_actions: int = 5) -> Engagement:
    if not hosts:
        hosts = ("a.local",)
    return Engagement(
        engagement_id=EngagementId("eng_loop_llm"),
        name="loop-llm",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        execution_budget=ExecutionBudget(max_actions=max_actions),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _loop_deps(
    engagement: Engagement,
    *,
    control: ControlPlane | None = None,
    finish: bool = True,
    gateway: DummyToolGateway | None = None,
    settings: OrchestrationSettings | None = None,
) -> RuntimeDeps:
    evidence = FakeEvidenceStore()
    llm = _LoopLLM(evidence, finish=finish)
    gw = gateway if gateway is not None else DummyToolGateway(tools=[h.inventory_descriptor()])
    deps = RuntimeDeps(
        control_plane=control or FakeControlPlane(),
        tool_gateway=gw,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gw,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=settings or OrchestrationSettings(max_steps=8, checkpointing=False),
    )
    return deps


async def test_build_runtime_deps_requires_explicit_llm(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="llm"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="LLM"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path, llm=None)  # type: ignore[arg-type]
    health = await build_runtime_deps(
        _engagement(), evidence_dir=tmp_path, llm=FakeLLMClient()
    )
    assert health.llm_client is not None


async def test_llm_single_probe_executes_original_behavior() -> None:
    engagement = _engagement()
    deps = _loop_deps(engagement)
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    llm = deps.llm_client
    assert isinstance(llm, _LoopLLM)
    final = await run_engagement(engagement, deps)
    assert final.actions_executed == 1
    assert [str(inv.tool_id) for inv in gateway.invocations] == [h.INVENTORY_TOOL]
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert len([r for r in evidence if isinstance(r.metadata.get("tool_result"), dict)]) == 1
    assert {"path_draft", "path_review", "next_action"} <= {c["route"] for c in llm.calls}


async def test_goal_preserved_in_accepted_path() -> None:
    engagement = _engagement()
    # New contract needs step + task + goal reviews: allow the shared
    # decision budget to cover the extra task turn (max 6, same as the
    # task-boundary reference flow).
    deps = _loop_deps(
        engagement,
        settings=OrchestrationSettings(max_steps=8, checkpointing=False, decision_max_attempts=6),
    )
    final = await run_engagement(engagement, deps)
    assert final.mission_id is not None
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None and mission.status is MissionStatus.COMPLETED
    path = await deps.path_store.get_active(str(final.run_id))
    assert path is not None and path.goal == mission.objective
    assert final.termination_reason == TerminationReason.GOAL_ACHIEVED
    # Thin regression: the executed tool call backs the mission purpose.
    # The gateway really ran inventory, and the settled criteria cite only
    # that stored tool evidence (no dummy ids, no invented claims).
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    assert [str(inv.tool_id) for inv in gateway.invocations] == [h.INVENTORY_TOOL]
    tool_ids = {
        str(r.evidence_id)
        for r in await deps.evidence_store.list_for_run(final.run_id)
        if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
    }
    assert tool_ids, "goal needs the executed call evidence"
    assert mission.criterion_results, "goal settlement keeps its evidence results"
    for result in mission.criterion_results:
        assert result.met and result.evidence_ids
        assert set(map(str, result.evidence_ids)) <= tool_ids
        assert result.result_claims, "each criterion keeps its actual result claims"
        for claim in result.result_claims:
            if isinstance(claim, dict):
                claimed = claim.get("evidence_ids", [])
            else:
                claimed = getattr(claim, "evidence_ids", [])
            assert set(map(str, claimed)) <= tool_ids


async def test_no_automatic_task_creation() -> None:
    engagement = _engagement()
    deps = _loop_deps(engagement)
    final = await run_engagement(engagement, deps)
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert len(tasks) == 1
    assert tasks[0].provenance.source == "engagement:goal"


async def test_policy_deny_blocks_execution_and_closes_lease() -> None:
    engagement = _engagement()
    deps = _loop_deps(
        engagement,
        control=FakeControlPlane(default_verdict=PolicyVerdict.DENY),
        finish=False,
    )
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    final = await run_engagement(engagement, deps)
    assert gateway.invocations == []
    assert final.actions_executed == 0
    assert EventType.TOOL_EXECUTION_STARTED not in bus.event_types()
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert work
    assert all(
        w.status not in (WorkStatus.PENDING, WorkStatus.LEASED, WorkStatus.RUNNING) for w in work
    )
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_approval_pending_suspends_with_partial_report() -> None:
    engagement = _engagement()
    deps = _loop_deps(
        engagement,
        control=FakeControlPlane(default_verdict=PolicyVerdict.REQUIRE_APPROVAL),
        finish=False,
    )
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.APPROVAL_PENDING
    assert final.pending_approval_id is not None
    assert gateway.invocations == []
    assert final.phase is AgentPhase.FINISHED
    projected = bus.history(event_type=REPORT_PROJECTED)
    assert len(projected) == 1
    assert projected[0].payload["ready_for_release"] is False


async def test_gateway_exception_normalized_and_closes_work() -> None:
    engagement = _engagement()
    evidence = FakeEvidenceStore()
    llm = _LoopLLM(evidence, finish=False)
    gateway = OneShotExplodingGateway()
    deps = RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=8, checkpointing=False),
    )
    final = await run_engagement(engagement, deps)
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert gateway.calls >= 1
    assert any(w.status is WorkStatus.FAILED for w in work)
    assert any(w.outcome_reason == "gateway_exception:RuntimeError" for w in work)
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    assert EventType.TOOL_EXECUTION_FAILED in bus.event_types()
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


class ConcurrencyTrackingGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__(tools=[h.inventory_descriptor()])
        self.active = 0
        self.max_active = 0

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.08)
            return await super().invoke(invocation)
        finally:
            self.active -= 1


class LeaseAwareSlowGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__(tools=[h.inventory_descriptor()])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return await super().invoke(invocation)


class BlockingGateway:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _BatchLLM:
    """Scripted model returning a fixed per-turn host plan, then hold."""

    def __init__(
        self,
        evidence: FakeEvidenceStore,
        action_plan: list[list[str]],
        *,
        vary_arguments: bool = False,
        agent_id: str = "recon-agent",
    ) -> None:
        self._evidence = evidence
        self._plan = action_plan
        self._vary_arguments = vary_arguments
        self._agent_id = agent_id
        self._seq = 0
        self._steps: dict[str, str] = {}
        for turn in action_plan:
            for host in turn:
                node = f"host:{host}"
                if node not in self._steps:
                    self._steps[node] = f"step-{len(self._steps) + 1}"
        self.calls: list[dict[str, Any]] = []
        self._turns = 0

    def _draft(self) -> dict[str, Any]:
        # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
        steps: list[dict[str, Any]] = [
            {
                    "step_id": step_id,
                    "objective": f"probe {node}",
                    "agent_id": self._agent_id,
                "target_node_ids": [node],
                "principal_refs": [],
                "allowed_operations": [h.CANONICAL_OP],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {"kind": "evidence", "description": "probe recorded", "min_evidence": 1}
                ],
                "required": True,
            }
            for node, step_id in self._steps.items()
        ]
        return {"reason": "scripted batch draft", "steps": steps}

    def _single(self, node: str) -> dict[str, Any]:
        attempt = self._seq
        self._seq += 1
        return {
            "path_step_id": self._steps[node],
            "agent_id": self._agent_id,
            "target_node_id": node,
            "principal_ref": None,
            "capability": "recon",
            "operation": "service_discovery",
            "tool_id": h.INVENTORY_TOOL,
            "arguments": {"attempt": attempt} if self._vary_arguments else {},
            "reason": f"inventory {node}",
            "expected_observation": "the inventory tool returns a result",
            "hypothesis": None,
            "existing_hypothesis_id": None,
            "expectation": None,
            "authentication_check": None,
            "availability_impact": False,
        }

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = _LoopLLM._route_key(response_schema)
        self.calls.append({"route": route})
        if route == "path_draft":
            return self._draft()
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "next_action":
            if self._turns < len(self._plan):
                hosts = self._plan[self._turns]
                self._turns += 1
                return {
                    "mode": "actions",
                    "reason": "scripted batch",
                    "actions": [self._single(f"host:{host}") for host in hosts],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return _LoopLLM._hold()
        raise AssertionError(f"no scripted answer for route {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


def _batch_deps(
    engagement: Engagement,
    action_plan: list[list[str]],
    *,
    gateway: DummyToolGateway | None = None,
    control: ControlPlane | None = None,
    settings: OrchestrationSettings | None = None,
    vary_arguments: bool = False,
    agents: AgentRegistry | None = None,
    agent_id: str = "recon-agent",
) -> RuntimeDeps:
    evidence = FakeEvidenceStore()
    llm = _BatchLLM(evidence, action_plan, vary_arguments=vary_arguments, agent_id=agent_id)
    gw = gateway if gateway is not None else DummyToolGateway(tools=[h.inventory_descriptor()])
    return RuntimeDeps(
        control_plane=control or FakeControlPlane(),
        tool_gateway=gw,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=evidence,
        llm_client=llm,
        engagement=engagement,
        agents=agents or default_registry(),
        tool_catalog=h.FakeCatalog([h.inventory_descriptor()]),
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=settings or OrchestrationSettings(max_steps=20, checkpointing=False),
    )


async def test_execution_budget_stops_the_loop() -> None:
    engagement = _engagement("a.local", "b.local", "c.local", max_actions=2)
    deps = _batch_deps(engagement, [["a.local"], ["b.local"], ["c.local"]])
    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert final.actions_executed == 2


async def test_turn_ceiling_stops_the_loop() -> None:
    engagement = _engagement("a.local", "b.local", "c.local")
    deps = _batch_deps(
        engagement,
        [["a.local"], ["b.local"], ["c.local"], ["a.local"]],
        settings=OrchestrationSettings(max_steps=2, checkpointing=False),
    )
    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.MAX_STEPS_REACHED
    assert final.step_count == 2


async def test_consecutive_failures_stop_hammering() -> None:
    engagement = _engagement("a.local", "b.local", "c.local")
    gateway = DummyToolGateway(success=False, tools=[h.inventory_descriptor()])
    deps = _batch_deps(
        engagement,
        [["a.local"], ["b.local"], ["c.local"], ["a.local"]],
        gateway=gateway,
        settings=OrchestrationSettings(max_consecutive_failures=2, checkpointing=False),
    )
    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.CONSECUTIVE_FAILURES
    assert final.actions_executed == 2


async def test_bounded_fan_out_executes_concurrently() -> None:
    hosts = [f"parallel-{index}.local" for index in range(5)]
    engagement = _engagement(*hosts, max_actions=10)
    gateway = ConcurrencyTrackingGateway()
    deps = _batch_deps(
        engagement,
        [[hosts[0], hosts[1], hosts[2]], [hosts[3], hosts[4]]],
        gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=3,
            max_actions_per_turn=3,
        ),
    )
    final = await run_engagement(engagement, deps)
    assert final.actions_executed == 5
    assert final.remaining_budget == 5
    assert gateway.max_active == 3
    assert len(gateway.invocations) == 5
    work = await deps.work_store.list_items()
    assert len(work) == 5
    assert all(item.status is WorkStatus.SUCCEEDED for item in work)


async def test_per_target_concurrency_serializes_one_host() -> None:
    # Two distinct behaviors on one host (different grounded args): at most one
    # is admitted per turn and the host never has two actions in flight, while
    # both still run across turns (global room allows two, the target gate does not).
    engagement = _engagement("a.local")
    gateway = ConcurrencyTrackingGateway()
    deps = _batch_deps(
        engagement,
        [["a.local", "a.local"], ["a.local"]],
        gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10,
            checkpointing=False,
            global_concurrency=2,
            max_actions_per_turn=2,
            per_target_concurrency=1,
        ),
        vary_arguments=True,
    )
    final = await run_engagement(engagement, deps)
    assert final.actions_executed == 2
    assert gateway.max_active == 1


async def test_long_running_tool_renews_its_work_lease() -> None:
    gateway = LeaseAwareSlowGateway()
    engagement = _engagement(max_actions=1)
    deps = _loop_deps(
        engagement,
        gateway=gateway,
        # This direct runner has no mission/task to complete; hold after execution.
        finish=False,
        settings=OrchestrationSettings(checkpointing=False, lease_ttl_seconds=0.06),
    )
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    task = asyncio.create_task(
        runner.run(
            RuntimeState(
                run_id=RunId("run_lease_llm"),
                engagement_id=engagement.engagement_id,
                current_goal="map the in-scope host",
                remaining_budget=1,
            )
        )
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=1.0)
    try:
        await asyncio.sleep(0.09)
        expired = await deps.work_store.reclaim_expired(now=datetime.now(timezone.utc))
        assert expired == []
    finally:
        gateway.release.set()
    final = await asyncio.wait_for(task, timeout=10.0)
    assert final.actions_executed == 1
    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
    work = await deps.work_store.list_items(run_id=final.run_id)
    assert work[0].status is WorkStatus.SUCCEEDED


async def test_parallel_worker_failure_isolated() -> None:
    engagement = _engagement("a.local", "b.local", max_actions=5)
    gateway = OneShotExplodingGateway()
    deps = _batch_deps(
        engagement,
        [["a.local", "b.local"]],
        gateway=gateway,
        settings=OrchestrationSettings(
            max_steps=10, checkpointing=False, global_concurrency=2, max_actions_per_turn=2
        ),
    )
    final = await run_engagement(engagement, deps)
    assert final.phase is AgentPhase.FINISHED
    assert final.actions_executed == 2
    assert gateway.calls == 2
    work = await deps.work_store.list_items()
    assert any(item.status is WorkStatus.FAILED for item in work)
    assert sum(item.status is WorkStatus.SUCCEEDED for item in work) == 1
    assert (await AttackGraphService(deps.graph_store).integrity_report()).blocking == []


async def test_stalled_no_progress_stops_the_loop() -> None:
    # Identical repeats are refused at compile time, so the stall script varies
    # only a dummy argument: every attempt compiles, the tool keeps answering
    # the same empty result, no new facts land, and the fingerprint stops moving.
    engagement = _engagement("a.local", max_actions=20)
    deps = _batch_deps(
        engagement,
        [["a.local"]] * 30,
        settings=OrchestrationSettings(max_steps=20, max_no_progress_turns=2, checkpointing=False),
        vary_arguments=True,
    )
    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.STALLED
    assert final.actions_executed < 20


async def test_every_turn_checkpointed_and_replayable() -> None:
    engagement = _engagement("a.local", "b.local")
    deps = _batch_deps(
        engagement,
        [["a.local"], ["b.local"]],
        settings=OrchestrationSettings(checkpointing=True),
    )
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    final = await runner.run(
        RuntimeState(
            run_id=RunId("run_replay_llm"),
            engagement_id=engagement.engagement_id,
            current_goal="map the in-scope host",
            remaining_budget=engagement.execution_budget.max_actions,
        )
    )
    assert final.actions_executed == 2
    history = await runner.state_history()
    assert len(history) > 1
    assert history[0].phase is AgentPhase.FINISHED
    assert [s.step_count for s in history] == sorted((s.step_count for s in history), reverse=True)
    earlier = next(s for s in reversed(history) if s.actions_executed == 1 and s.next_nodes)
    resumed = await runner.resume_from(earlier.checkpoint_id)
    assert resumed.phase is AgentPhase.FINISHED
    assert resumed.actions_executed >= earlier.actions_executed


async def test_sqlite_checkpoint_history_survives(tmp_path: Path) -> None:
    settings = OrchestrationSettings(
        checkpoint_backend="sqlite",
        checkpoint_path=str(tmp_path / "runner.sqlite3"),
        thread_id="run_sqlite_llm",
    )
    engagement = _engagement("a.local")
    deps = _batch_deps(engagement, [["a.local"]], settings=settings)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    first = AgentLoopRunner(deps, settings=settings)
    await first.run(
        RuntimeState(
            run_id=RunId("run_sqlite_llm"),
            engagement_id=engagement.engagement_id,
            current_goal="map the in-scope host",
            remaining_budget=2,
        )
    )
    await first.aclose()
    reopened = AgentLoopRunner(deps, settings=settings)
    history = await reopened.state_history()
    await reopened.aclose()
    assert history
    assert history[0].phase is AgentPhase.FINISHED


async def test_capped_agent_budget_refuses_further_approval() -> None:
    capped = RECON_AGENT.model_copy(
        deep=True, update={"agent_id": AgentId("capped-agent"), "max_actions": 1}
    )
    hosts = ["cap-a.local", "cap-b.local", "cap-c.local"]
    engagement = _engagement(*hosts, max_actions=10)
    deps = _batch_deps(
        engagement,
        [[host] for host in hosts],
        agents=AgentRegistry([capped]),
        agent_id="capped-agent",
        settings=OrchestrationSettings(max_steps=10, checkpointing=False),
    )
    final = await run_engagement(engagement, deps)
    assert final.actions_executed == 1
    assert final.actions_by_agent == {"capped-agent": 1}
    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
