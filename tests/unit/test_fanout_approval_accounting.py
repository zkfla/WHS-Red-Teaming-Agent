"""Fan-out AGGREGATE accounting: approval binding and retry folding.

The sequential path never reaches AGGREGATE; these defects are only
reachable once ``max_actions_per_turn > 1`` opens the LangGraph ``Send``
fan-out:

* A batch where one worker returns ``REQUIRE_APPROVAL`` used to leave
  ``runtime.pending_action_id`` at ``None`` (the old aggregate) and leave the
  checkpoint carrying whatever proposal PLAN had selected -- not the one the
  human is being asked to approve. The durable resume path re-enters at
  POLICY only when the checkpointed ``action_proposal.action_id`` matches
  ``runtime.pending_action_id`` (``AgentLoopRunner._resume_pending_approval``);
  a mismatch fails closed, so the approval could never be honored.
* ``retry_attempts`` reported by each worker must be folded into the durable
  ``retry_tool_calls`` counter and the remaining budget exactly as
  ``execute_node`` does on the sequential path -- once per action, plus one
  slot per extra attempt.

Scope is targeted: the AGGREGATE node in isolation, its hand-off into
REPLAN's terminal branch (what actually gets checkpointed), and one real
graph fan-out resumed through ``AgentLoopRunner`` against a fake Control
Plane. No E2E/browser execution.
"""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.types import Overwrite

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.events import EventType
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import (
    AgentPhase,
    GraphNodeType,
    PolicyVerdict,
    RiskLevel,
    ToolHealthStatus,
    Verdict,
)
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    EngagementId,
    EventId,
    HypothesisId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import PlanStep
from redteam.orchestration.dispatch import DispatchItem, WorkerOutcome
from redteam.orchestration.nodes import aggregate_node, replan_node
from redteam.orchestration.nodes.plan import _verification_batch_capacity
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
)
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application

RUN = RunId("run_fanout")
ENGAGEMENT = EngagementId("eng_fanout")
NOW = datetime.now(timezone.utc)


def test_fanout_admission_keeps_verification_and_retry_slots_available() -> None:
    settings = OrchestrationSettings(
        max_actions_per_turn=3, max_retries=0,
        enforce_verification_reserve=True, verification_reserve_ratio=0.5,
    )
    assert _verification_batch_capacity(runtime(remaining_budget=4), settings) == 2
    assert _verification_batch_capacity(runtime(remaining_budget=2), settings) == 1
    with_retries = settings.model_copy(update={"max_retries": 3})
    assert _verification_batch_capacity(runtime(remaining_budget=4), with_retries) == 1


def runtime(**overrides: object) -> RuntimeState:
    return RuntimeState.model_validate({"run_id": RUN, "engagement_id": ENGAGEMENT, **overrides})


def plan_step(step_id: str, host: str) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.probe"),
        target=HostTarget(host=host),
        reason="fan-out accounting",
    )


def proposal(
    action_id: str,
    host: str,
    *,
    agent_id: str = "recon-agent",
    hypothesis_id: HypothesisId | None = None,
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AgentId(agent_id),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        reason="fan-out accounting",
        hypothesis_id=hypothesis_id,
        created_at=NOW,
    )


def dispatch_item(
    action_id: str,
    host: str,
    *,
    agent_id: str = "recon-agent",
    hypothesis_id: HypothesisId | None = None,
) -> DispatchItem:
    return DispatchItem(
        work_id=f"work_{action_id}",
        plan_step=plan_step(action_id, host),
        proposal=proposal(action_id, host, agent_id=agent_id, hypothesis_id=hypothesis_id),
    )


def state_with(rt: RuntimeState, **transient: object) -> GraphState:
    base: dict[str, object] = dict(initial_graph_state(rt))
    base.update(transient)
    return base  # type: ignore[return-value]


# -- AGGREGATE: approval binding -----------------------------------------


async def test_second_item_approval_binds_the_exact_pending_action() -> None:
    """The human must approve the action the second worker actually asked
    about, not the first/selected one PLAN admitted."""
    deps = build_test_application()
    allowed = dispatch_item("action_allowed", "b.local")
    pending = dispatch_item(
        "action_pending",
        "a.local",
        hypothesis_id=HypothesisId("hyp_pending"),
    )
    state = state_with(
        runtime(actions_executed=1, remaining_budget=5, retry_tool_calls=2),
        dispatch_items=[allowed, pending],
        # What PLAN selected before the fan-out: the other action.
        action_proposal=allowed.proposal,
        plan_step=allowed.plan_step,
        worker_outcomes={
            "action_allowed": WorkerOutcome(
                action_id="action_allowed",
                work_id=allowed.work_id,
                executed=True,
                succeeded=True,
                verdict=Verdict.PASS,
                last_event_id=EventId("ev_allowed"),
            ),
            "action_pending": WorkerOutcome(
                action_id="action_pending",
                work_id=pending.work_id,
                policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
                approval_id=ApprovalId("appr_action_pending"),
                last_event_id=EventId("ev_pending"),
            ),
        },
    )

    result = await aggregate_node(state, deps=deps)
    rt = result["runtime"]

    assert rt.pending_action_id == pending.proposal.action_id
    assert rt.pending_action_id != allowed.proposal.action_id
    assert rt.pending_approval_id == ApprovalId("appr_action_pending")
    assert rt.termination_reason == TerminationReason.APPROVAL_PENDING

    # Only the allowed sibling executed in this batch.
    assert rt.actions_executed == 2
    assert rt.remaining_budget == 4
    assert rt.retry_tool_calls == 2

    # The exact proposal/plan step the resume path will re-enter POLICY with.
    assert result["action_proposal"] == pending.proposal
    assert result["plan_step"] == pending.plan_step
    # The batch is consumed: no stale reducer rows may outlive the turn.
    # AGGREGATE returns an explicit ``Overwrite`` so the merge reducer replaces
    # the map instead of merging a no-op.
    assert result["dispatch_items"] == []
    assert result["worker_outcomes"] == Overwrite(value={})
    assert result["last_event_id"] == EventId("ev_pending")


async def test_first_item_approval_keeps_that_action_and_still_folds_siblings() -> None:
    """Approval on the selected (first) action is not overwritten, and a
    later executed sibling is still counted."""
    deps = build_test_application()
    pending = dispatch_item("action_first", "a.local")
    allowed = dispatch_item("action_second", "b.local")
    state = state_with(
        runtime(actions_executed=0, remaining_budget=3),
        dispatch_items=[pending, allowed],
        action_proposal=pending.proposal,
        plan_step=pending.plan_step,
        worker_outcomes={
            "action_first": WorkerOutcome(
                action_id="action_first",
                work_id=pending.work_id,
                policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
                approval_id=ApprovalId("appr_action_first"),
            ),
            "action_second": WorkerOutcome(
                action_id="action_second",
                work_id=allowed.work_id,
                executed=True,
                succeeded=True,
                verdict=Verdict.PASS,
            ),
        },
    )

    result = await aggregate_node(state, deps=deps)
    rt = result["runtime"]

    assert rt.pending_action_id == pending.proposal.action_id
    assert rt.pending_approval_id == ApprovalId("appr_action_first")
    assert result["action_proposal"] == pending.proposal
    assert result["plan_step"] == pending.plan_step
    assert rt.actions_executed == 1
    assert rt.remaining_budget == 2


async def test_unexecuted_workers_consume_no_budget_or_retry_slots() -> None:
    """An approval and a denial never ran: neither may debit budget,
    retry_tool_calls, or the per-agent action count."""
    deps = build_test_application()
    denied = dispatch_item("action_denied", "b.local", agent_id="agent_b")
    pending = dispatch_item("action_pending", "a.local", agent_id="agent_a")
    state = state_with(
        runtime(actions_executed=3, remaining_budget=7, retry_tool_calls=1),
        dispatch_items=[denied, pending],
        worker_outcomes={
            "action_denied": WorkerOutcome(
                action_id="action_denied",
                work_id=denied.work_id,
                policy_verdict=PolicyVerdict.DENY,
            ),
            "action_pending": WorkerOutcome(
                action_id="action_pending",
                work_id=pending.work_id,
                policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
                approval_id=ApprovalId("appr_action_pending"),
            ),
        },
    )

    result = await aggregate_node(state, deps=deps)
    rt = result["runtime"]

    assert rt.actions_executed == 3
    assert rt.remaining_budget == 7
    assert rt.retry_tool_calls == 1
    assert rt.actions_by_agent == {}
    assert rt.pending_action_id == pending.proposal.action_id


# -- AGGREGATE: retry-attempt folding ------------------------------------


async def test_retry_attempts_fold_into_durable_counters_exactly_once() -> None:
    """One budget slot per executed action plus one per extra attempt, and
    ``retry_tool_calls`` tracks only the extra attempts."""
    deps = build_test_application()
    first = dispatch_item("action_first", "a.local", agent_id="agent_a")
    second = dispatch_item("action_second", "b.local", agent_id="agent_b")
    state = state_with(
        runtime(
            actions_executed=4,
            remaining_budget=10,
            retry_tool_calls=3,
            consecutive_failures=2,
        ),
        dispatch_items=[first, second],
        worker_outcomes={
            "action_first": WorkerOutcome(
                action_id="action_first",
                work_id=first.work_id,
                executed=True,
                succeeded=True,
                retry_attempts=2,
                verdict=Verdict.PASS,
            ),
            "action_second": WorkerOutcome(
                action_id="action_second",
                work_id=second.work_id,
                executed=True,
                succeeded=False,
                retry_attempts=1,
                verdict=Verdict.UNKNOWN,
            ),
        },
    )

    result = await aggregate_node(state, deps=deps)
    rt = result["runtime"]

    assert rt.actions_executed == 6
    assert rt.retry_tool_calls == 3 + (2 + 1)
    # (1 + 2) + (1 + 1) = 5 consumed.
    assert rt.remaining_budget == 5
    assert rt.actions_by_agent == {"agent_a": 1, "agent_b": 1}
    # A success in the batch resets the back-to-back failure streak.
    assert rt.consecutive_failures == 0


async def test_retry_attempts_do_not_count_unexecuted_workers() -> None:
    """A worker that never ran must report zero retries, even if the
    coordinator's stale ``retry_count`` was non-zero before the batch."""
    deps = build_test_application()
    pending = dispatch_item("action_pending", "a.local")
    state = state_with(
        runtime(actions_executed=0, remaining_budget=5, retry_count=3),
        dispatch_items=[pending],
        worker_outcomes={
            "action_pending": WorkerOutcome(
                action_id="action_pending",
                work_id=pending.work_id,
                # A worker outcome without executed=True defaults to no retries.
                policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
                approval_id=ApprovalId("appr_action_pending"),
            )
        },
    )

    result = await aggregate_node(state, deps=deps)
    rt = result["runtime"]

    assert rt.retry_tool_calls == 0
    assert rt.remaining_budget == 5
    assert rt.actions_executed == 0


async def test_fanout_honors_the_verification_reserve_like_sequential_execute() -> None:
    """``execute_node`` zeroes the remaining budget once spending reaches the
    verification reserve. Each worker's local budget is discarded with its
    isolated state, so AGGREGATE must reproduce that decision for the batch
    or the fan-out path silently drains the reserved slice."""
    reserve_on = build_test_application(
        settings=OrchestrationSettings(
            enforce_verification_reserve=True,
            verification_reserve_ratio=0.5,
        )
    )
    reserve_off = build_test_application()
    item = dispatch_item("action_a", "a.local")
    state = state_with(
        runtime(actions_executed=0, remaining_budget=2),
        dispatch_items=[item],
        worker_outcomes={
            "action_a": WorkerOutcome(
                action_id="action_a",
                work_id=item.work_id,
                executed=True,
                succeeded=True,
                verdict=Verdict.PASS,
            )
        },
    )

    with_reserve = await aggregate_node(state, deps=reserve_on)
    without_reserve = await aggregate_node(state, deps=reserve_off)

    # total_budget=2 -> reserve=ceil(2 * 0.5)=1; spending one slot lands on
    # the reserve, so the slice is held back and the run stops.
    assert with_reserve["runtime"].remaining_budget == 0
    # The legacy/default path is unchanged: no reserve, one slot spent.
    assert without_reserve["runtime"].remaining_budget == 1


# -- AGGREGATE -> REPLAN: what actually gets checkpointed ----------------


async def test_pending_proposal_and_plan_step_survive_the_terminal_replan() -> None:
    """The checkpoint the durable resume reads is written after REPLAN's
    terminal branch, so the pending proposal/step must still be there."""
    deps = build_test_application()
    allowed = dispatch_item("action_allowed", "b.local")
    pending = dispatch_item(
        "action_pending",
        "a.local",
        hypothesis_id=HypothesisId("hyp_pending"),
    )
    state = state_with(
        runtime(actions_executed=1, remaining_budget=5),
        dispatch_items=[allowed, pending],
        action_proposal=allowed.proposal,
        plan_step=allowed.plan_step,
        worker_outcomes={
            "action_allowed": WorkerOutcome(
                action_id="action_allowed",
                work_id=allowed.work_id,
                executed=True,
                succeeded=True,
                verdict=Verdict.PASS,
            ),
            "action_pending": WorkerOutcome(
                action_id="action_pending",
                work_id=pending.work_id,
                policy_verdict=PolicyVerdict.REQUIRE_APPROVAL,
                approval_id=ApprovalId("appr_action_pending"),
            ),
        },
    )

    aggregated = await aggregate_node(state, deps=deps)
    replanned = await replan_node(aggregated, deps=deps)

    assert replanned["runtime"].termination_reason == TerminationReason.APPROVAL_PENDING
    assert replanned["runtime"].phase is AgentPhase.FINISHED
    assert replanned["runtime"].pending_action_id == pending.proposal.action_id
    assert replanned["runtime"].pending_approval_id == ApprovalId("appr_action_pending")
    assert replanned["action_proposal"] == pending.proposal
    assert replanned["plan_step"] == pending.plan_step


# -- Runner: real fan-out through a fake Control Plane -------------------


class _SecondProposalApprovalPlane(FakeControlPlane):
    """A fake Control Plane that requires approval for exactly the second
    ``ACTION_PROPOSED`` of the run.

    Reading the Event Bus (populated by PLAN before workers evaluate) makes
    "the second item in the batch" independent of evaluation or worker
    completion order -- the property AGGREGATE's ordering must respect.
    """

    def __init__(self, event_bus: InMemoryEventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    async def evaluate(self, proposal, context):  # noqa: ANN001, ANN201
        self.evaluated.append(proposal)
        proposed = [
            event.payload["action_id"]
            for event in self._event_bus.history(event_type=EventType.ACTION_PROPOSED)
        ]
        second = proposed[1] if len(proposed) > 1 else None
        if second is not None and str(proposal.action_id) == second:
            return PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=["second-proposal-approval"],
                matched_rules=[],
                approval_id=ApprovalId(f"appr_{proposal.action_id}"),
                evaluated_at=NOW,
            )
        return PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.ALLOW,
            reasons=["fake-allow"],
            matched_rules=[],
            evaluated_at=NOW,
        )


class _HostApprovalPlane(FakeControlPlane):
    async def evaluate(self, proposal, context):  # noqa: ANN001, ANN201
        self.evaluated.append(proposal)
        host = getattr(proposal.target, "host", None)
        if host == "b.local":
            return PolicyDecision(
                action_id=proposal.action_id,
                verdict=PolicyVerdict.REQUIRE_APPROVAL,
                reasons=["b-host-approval"],
                matched_rules=[],
                approval_id=ApprovalId(f"appr_{proposal.action_id}"),
                evaluated_at=NOW,
            )
        return PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.ALLOW,
            reasons=["fake-allow"],
            matched_rules=[],
            evaluated_at=NOW,
        )


class _ScriptedLLM:
    def __init__(self) -> None:
        self._routes: dict[str, list[object]] = {}
        self.calls: list[dict[str, object]] = []

    def add(self, route: str, response: object) -> None:
        self._routes.setdefault(route, []).append(response)

    @staticmethod
    def _route_key(schema: dict) -> str:
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

    async def complete_structured(self, *, messages, response_schema, system=None, **_: object):
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        queue = self._routes.get(route, [])
        if not queue:
            raise AssertionError(f"no scripted response left for route {route!r}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(self, *, messages, system=None, **_: object) -> str:
        return ""


def _fanout_probe_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("recon.probe"),
        server_id="fanout-test",
        tool_name="probe",
        description="fanout approval test probe",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        operations=["service_discovery"],
        capability_tags=["recon"],
        target_types=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.UNKNOWN,
    )


def _fanout_path_draft(node_a: str, node_b: str) -> dict:
    # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
    return {
        "reason": "scripted fanout draft",
        "steps": [
            {
                "step_id": "step-1",
                "objective": "probe both fanout hosts",
                "agent_id": "recon-agent",
                "target_node_ids": [node_a, node_b],
                "principal_refs": [],
                "allowed_operations": ["recon.service_discovery"],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {"kind": "evidence", "description": "fanout probe", "min_evidence": 1}
                ],
                "required": True,
            }
        ],
    }


def _fanout_two_actions(node_a: str, node_b: str) -> dict:
    def _intent(node_id: str) -> dict:
        return {
            "path_step_id": "step-1",
            "agent_id": "recon-agent",
            "target_node_id": node_id,
            "principal_ref": None,
            "capability": "recon",
            "operation": "service_discovery",
            "tool_id": "recon.probe",
            "arguments": {},
            "reason": "fan-out probe",
            "expected_observation": "probe returns",
            "hypothesis": None,
            "existing_hypothesis_id": None,
            "expectation": None,
            "authentication_check": None,
            "availability_impact": False,
        }
    return {
        "mode": "actions",
        "reason": "scripted fan-out pair",
        "actions": [_intent(node_a), _intent(node_b)],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def _fanout_hold() -> dict:
    return {
        "mode": "hold",
        "reason": "scripted hold after resume",
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


async def test_fanout_second_item_approval_resumes_that_exact_action() -> None:
    bus = InMemoryEventBus()
    llm = _ScriptedLLM()
    node_a = "host:a.local"
    node_b = "host:b.local"
    llm.add("path_draft", _fanout_path_draft(node_a, node_b))
    llm.add("path_review", {"decision": "accept", "reason": "scripted accept", "path": None})
    llm.add("next_action", _fanout_two_actions(node_a, node_b))
    llm.add("next_action", _fanout_hold())
    plane = _HostApprovalPlane()
    settings = OrchestrationSettings(
        checkpointing=True,
        max_steps=10,
        global_concurrency=2,
        max_actions_per_turn=2,
    )
    gateway = DummyToolGateway(tools=[_fanout_probe_descriptor()])
    deps = build_test_application(
        control_plane=plane,
        tool_gateway=gateway,
        tool_catalog=gateway,
        event_bus=bus,
        llm_client=llm,
        settings=settings,
    )
    graph = AttackGraphService(deps.graph_store)
    for host in ("a.local", "b.local"):
        await graph.upsert_node(
            GraphNodeType.HOST,
            key=host,
            attrs={"in_scope": True},
            labels=["engagement-scope"],
            provenance=Provenance(
                step=0,
                source="engagement:test",
                run_id="run_fanout_resume",
                confidence=Confidence.SERVER_DECLARED,
            ),
            step=0,
        )

    runner = AgentLoopRunner(deps, settings=settings)
    try:
        stopped = await runner.run(
            RuntimeState(
                run_id=RunId("run_fanout_resume"),
                engagement_id=ENGAGEMENT,
                remaining_budget=4,
                current_goal="probe both hosts",
            )
        )
        assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
        assert stopped.pending_action_id is not None
        assert stopped.pending_approval_id is not None

        proposed = [
            event.payload["action_id"]
            for event in bus.history(event_type=EventType.ACTION_PROPOSED)
        ]
        # The second proposal is the one held for approval, and exactly one
        # sibling executed in the same batch.
        assert proposed[1] == str(stopped.pending_action_id)
        assert stopped.actions_executed == 1

        rows = await deps.work_store.list_items(run_id=RunId("run_fanout_resume"))
        assert len(rows) == 2
        assert len({item.work_id for item in rows}) == 2
        by_target = {
            (item.target_node_ids[0] if item.target_node_ids else ""): item for item in rows
        }
        assert by_target[node_a].status is WorkStatus.SUCCEEDED
        assert by_target[node_b].status is WorkStatus.PENDING
        snapshot = await runner._latest_snapshot()  # noqa: SLF001 - focused checkpoint check
        assert snapshot is not None
        proposal_in_checkpoint = snapshot.values.get("action_proposal")
        step_in_checkpoint = snapshot.values.get("plan_step")
        assert proposal_in_checkpoint is not None
        assert proposal_in_checkpoint.action_id == stopped.pending_action_id
        assert step_in_checkpoint is not None
        # ``recover_runtime`` is the durable-resume entry point and refuses a
        # snapshot whose next super-step would replay execution.
        assert await runner.recover_runtime("run_fanout_resume") is not None
        assert snapshot.values.get("current_work_id") == by_target[node_b].work_id

        plane.resolve_approval(
            stopped.pending_approval_id,
            PolicyDecision(
                action_id=stopped.pending_action_id,
                verdict=PolicyVerdict.ALLOW,
                reasons=["approved"],
                matched_rules=[],
                evaluated_at=NOW,
            ),
        )
        recovered = await runner.recover_runtime("run_fanout_resume")
        assert recovered is not None
        final = await runner.resume_recovered(recovered)
    finally:
        await runner.aclose()

    # The approved second action actually ran, on top of the sibling.
    assert final.actions_executed == 2
    assert final.pending_action_id is None
    assert final.pending_approval_id is None
    invoked = {str(invocation.action_id) for invocation in gateway.invocations}
    assert str(stopped.pending_action_id) in invoked
    assert set(proposed) <= invoked
    assert len(gateway.invocations) == 2
    settled = await deps.work_store.list_items(run_id=RunId("run_fanout_resume"))
    assert len(settled) == 2
    assert {item.status for item in settled} == {WorkStatus.SUCCEEDED}
    assert {item.work_id for item in rows} == {item.work_id for item in settled}
