"""Stage 6: structured explanation evidence for the operator console.

Prompt 4/6 asks that "why was this action chosen / why was this candidate
excluded / why is this task blocked / why is this endpoint not seen" be
answerable from *structured observable state*: the candidate score, the
policy reason, the Work Ledger row, and an event reference — no raw secret
text, no chain-of-thought, and never evidence from another run.

These tests exercise the two owned modules only
(``operator_intents.service`` / ``operator_intents.models``); the renderer
seam they feed is noted in the parent handoff, not edited here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.app.cli.renderer import operator_explanation_text
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import AgentId, EngagementId, GraphNodeId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    OperatorIntentService,
)
from redteam.orchestration.operator_intents.models import (
    ActionExplanation,
    CandidateExplanation,
)
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = RunId("run_stage6")
OTHER_RUN = RunId("run_stage6_other")
ENG = EngagementId("eng_stage6")
MISSION = MissionId("mission_stage6")


def _mission() -> MissionPlan:
    return MissionPlan(
        mission_id=MISSION,
        engagement_id=ENG,
        objective="Assess the declared scope",
        success_criteria=["evidence-backed result"],
        status=MissionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


def _task(
    task_id: str, *, capability: str = "api", objective: str = "Probe /api/docs"
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MISSION,
        title=task_id,
        objective=objective,
        status=TaskStatus.READY,
        priority=10,
        required_capability=capability,
        assigned_agent=AgentId("web-api-agent"),
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


async def _service(
    *,
    bus: InMemoryEventBus | None = None,
    work_store: InMemoryWorkStore | None = None,
) -> OperatorIntentService:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())
    await store.create_task(_task("docs"))
    return OperatorIntentService(
        store=InMemoryOperatorIntentStore(),
        task_graph_store=store,
        event_bus=bus,
        work_store=work_store,
    )


def _publisher(bus: InMemoryEventBus, run_id: RunId = RUN) -> EventPublisher:
    return EventPublisher(bus, run_id=run_id, engagement_id=ENG, source="test.stage6")


# --- action chosen: proposal score + policy reason linked by action id -----


async def test_action_explanation_joins_proposal_score_with_approved_policy_reason() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    pub = _publisher(bus)
    await pub.publish(
        EventType.ACTION_PROPOSED,
        {
            "action_id": "act-1",
            "operation": "web.inspect",
            "candidate_key": "k-web",
            "target": "a.local",
            "work_id": "w-1",
            "priority": 42.5,
            "priority_components": {"impact": 0.9, "operational_risk": 0.1, "note": "x"},
        },
    )
    await pub.publish(
        EventType.POLICY_EVALUATED,
        {
            "action_id": "act-1",
            "verdict": "allow",
            "matched_rules": ["roe:in_scope"],
            "budget_remaining": 3,
        },
    )
    await pub.publish(
        EventType.ACTION_APPROVED,
        {"action_id": "act-1", "reasons": ["within roe"], "approval_id": None},
    )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN, subjects=["action"])

    assert explanation.chosen_actions
    action = explanation.chosen_actions[0]
    assert isinstance(action, ActionExplanation)
    assert action.action_id == "act-1"
    assert action.work_id == "w-1"
    assert action.priority == 42.5
    # Non-numeric score components are dropped, the rest are bounded references.
    assert action.priority_components == {"impact": 0.9, "operational_risk": 0.1}
    assert action.policy_verdict == "allow"
    assert action.policy_reasons == ["within roe"]
    assert action.matched_rules == ["roe:in_scope"]
    assert action.evidence_event_id is not None
    assert "score 42.5" in explanation.summary
    assert "within roe" in explanation.summary
    rendered = operator_explanation_text(explanation)
    assert "chosen web.inspect: allow score=42.5 policy=allow (within roe)" in rendered


async def test_denied_and_approved_actions_stay_linked_to_their_own_action_id() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    pub = _publisher(bus)
    await pub.publish(EventType.ACTION_PROPOSED, {"action_id": "act-a", "operation": "alpha"})
    await pub.publish(EventType.ACTION_PROPOSED, {"action_id": "act-b", "operation": "beta"})
    await pub.publish(EventType.ACTION_APPROVED, {"action_id": "act-a", "reasons": ["ok"]})
    await pub.publish(EventType.ACTION_DENIED, {"action_id": "act-b", "reasons": ["off scope"]})

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN, subjects=["action"])

    by_id = {action.action_id: action for action in explanation.chosen_actions}
    assert by_id["act-a"].status == "allow"
    assert by_id["act-a"].policy_reasons == ["ok"]
    assert by_id["act-b"].status == "deny"
    assert by_id["act-b"].policy_reasons == ["off scope"]


async def test_policy_reasons_are_redacted_and_bounded() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    pub = _publisher(bus)
    await pub.publish(EventType.ACTION_PROPOSED, {"action_id": "act-secret", "operation": "probe"})
    await pub.publish(
        EventType.ACTION_DENIED,
        {
            "action_id": "act-secret",
            "reasons": [
                "denied because token=supersecret",
                *[f"reason-{index}" for index in range(20)],
            ],
        },
    )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN, subjects=["action"])
    action = explanation.chosen_actions[0]

    assert len(action.policy_reasons) == 5  # bounded
    assert "supersecret" not in action.model_dump_json()
    assert "***REDACTED***" in action.policy_reasons[0]


async def test_policy_evidence_never_leaks_across_runs_on_a_shared_bus() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    current = _publisher(bus)
    other = _publisher(bus, run_id=OTHER_RUN)
    # Proposed in *this* run, but approved only in the other run.
    await current.publish(
        EventType.ACTION_PROPOSED, {"action_id": "act-1", "operation": "web.inspect"}
    )
    await other.publish(
        EventType.ACTION_APPROVED, {"action_id": "act-1", "reasons": ["other-run secret"]}
    )
    await other.publish(
        EventType.ACTION_PROPOSED, {"action_id": "act-2", "operation": "private.other"}
    )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN, subjects=["action"])

    assert [action.operation for action in explanation.chosen_actions] == ["web.inspect"]
    assert explanation.chosen_actions[0].status == "proposed"
    assert explanation.chosen_actions[0].policy_reasons == []


async def test_proposed_action_explanations_are_bounded() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    pub = _publisher(bus)
    for index in range(9):
        await pub.publish(
            EventType.ACTION_PROPOSED, {"action_id": f"act-{index}", "operation": "op"}
        )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN)

    assert len(explanation.chosen_actions) == 5


# --- candidate excluded + work-ledger score -------------------------------


async def test_work_ledger_fallback_exposes_candidate_score_and_reason() -> None:
    work_store = InMemoryWorkStore()
    common: dict[str, Any] = dict(
        run_id=RUN,
        agent_id=AgentId("web-api-agent"),
        target_node_ids=[GraphNodeId("endpoint:a.local GET /api/docs")],
        created_at=NOW,
        updated_at=NOW,
    )
    await work_store.add(
        WorkItem(
            work_id="w-done",
            candidate_key="web.inspect",
            status=WorkStatus.SUCCEEDED,
            outcome_reason="done",
            priority=PriorityScore(total=3.5, components={"impact": 0.5}),
            **common,
        )
    )
    await work_store.add(
        WorkItem(
            work_id="w-blocked",
            candidate_key="api.probe",
            status=WorkStatus.BLOCKED,
            outcome_reason="prerequisite_not_succeeded:w0",
            priority=PriorityScore(total=1.25),
            **common,
        )
    )
    svc = await _service(work_store=work_store)

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN)

    chosen = explanation.chosen_actions[0]
    rejected = explanation.rejected_candidates[0]
    assert isinstance(chosen, ActionExplanation)
    assert isinstance(rejected, CandidateExplanation)
    assert chosen.operation == "web.inspect"
    assert chosen.work_id == "w-done"
    assert chosen.priority == 3.5
    assert rejected.operation == "api.probe"
    assert rejected.priority == 1.25
    assert "prerequisite_not_succeeded" in (rejected.reason or "")


async def test_rejected_candidate_from_event_carries_an_evidence_reference() -> None:
    bus = InMemoryEventBus()
    svc = await _service(bus=bus)
    await _publisher(bus).publish(
        "CandidateRejected",
        {
            "operation": "recon.probe",
            "reason": "prior_terminal_execution",
            "candidate_key": "k1",
        },
    )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN, subjects=["candidate"])

    candidate = explanation.rejected_candidates[0]
    assert candidate.evidence_event_id is not None
    assert candidate.priority is None


# --- endpoint not seen + blocked task still answered structurally ----------


async def test_endpoint_not_seen_and_blocked_task_are_structured_evidence() -> None:
    task_store = InMemoryTaskGraphStore()
    await task_store.create_mission(_mission())
    await task_store.create_task(_task("docs"))
    await task_store.create_task(
        MissionTask(
            task_id=TaskId("blocked-docs"),
            mission_id=MISSION,
            title="blocked-docs",
            objective="Probe /api/orders",
            status=TaskStatus.BLOCKED,
            priority=10,
            required_capability="api",
            dependency_task_ids=[TaskId("docs")],
            blocking_reason="waiting on fingerprint token=do-not-print",
            assigned_agent=AgentId("web-api-agent"),
            provenance=Provenance(source="engagement:bootstrap"),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    svc = OperatorIntentService(
        store=InMemoryOperatorIntentStore(),
        task_graph_store=task_store,
    )

    explanation = await svc.explain(
        mission_id=MISSION,
        endpoint_selectors=["/api/absent"],
        subjects=["blocked", "endpoint"],
    )

    assert explanation.endpoint_matches == []
    assert "not recorded in the attack graph" in explanation.summary
    assert explanation.blocked_tasks
    blocked = explanation.blocked_tasks[0]
    assert blocked.blocked_dependency_ids == [TaskId("docs")]
    assert "***REDACTED***" in blocked.blocking_reason
    assert "do-not-print" not in explanation.model_dump_json()
