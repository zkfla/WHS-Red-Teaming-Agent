"""Terminal reason preservation, safe failure feedback, and ordered recovery."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from redteam.app.bootstrap import settle_engagement
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.completion import CompletionIssues
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    OperatorIntentService,
)
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.state import RuntimeState, TerminationReason
from tests.fakes import build_test_application


async def setup():
    deps = replace(build_test_application(), operator_intent_store=InMemoryOperatorIntentStore())
    now = datetime.now(timezone.utc)
    await deps.task_graph_store.create_mission(
        MissionPlan(
            mission_id="mission-final",
            engagement_id="eng-final",
            objective="Inspect authorized target",
            status=MissionStatus.CANCELLED,
            provenance=Provenance(source="test"),
            created_at=now,
            updated_at=now,
        )
    )
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    await coordinator.bind("run-final", "mission-final", "eng-final")
    runtime = RuntimeState(
        run_id="run-final",
        mission_id="mission-final",
        engagement_id="eng-final",
        phase=AgentPhase.FINISHED,
        termination_reason=TerminationReason.USER_INTERRUPTED,
    )
    publisher = EventPublisher(
        deps.event_bus, run_id=runtime.run_id, engagement_id=runtime.engagement_id, source="test"
    )
    return deps, coordinator, runtime, publisher


@pytest.mark.parametrize("late_stop", [False, True])
async def test_final_settlement_preserves_previous_cancel_reason(monkeypatch, late_stop):
    deps, _, runtime, publisher = await setup()
    if late_stop:
        service = OperatorIntentService.from_deps(deps)
        result = await service.receive_and_apply(
            "stop", mission_id=runtime.mission_id, run_id=runtime.run_id, publisher=publisher
        )
        assert not result.applied

    async def project(*args, **kwargs):
        return None

    monkeypatch.setattr("redteam.app.bootstrap._publish_terminal_artifacts", project)
    settled = await settle_engagement(runtime, deps)
    assert settled.termination_reason == TerminationReason.USER_INTERRUPTED
    assert not deps.event_bus.history(event_type=EventType.OPERATOR_INTENT_APPLIED)


async def test_more_than_one_delivery_batch_retains_original_event_order():
    deps, coordinator, runtime, publisher = await setup()

    async def queue(change):
        for index in range(205):
            change.queue(publisher.build(EventType.MISSION_UPDATED, {"index": index}))

    await coordinator.change(queue)
    pending = await deliver_pending(
        coordinator,
        run_id=str(runtime.run_id),
        engagement_id=str(runtime.engagement_id),
        event_bus=deps.event_bus,
        budget_seconds=10,
    )
    assert pending == 0
    assert [event.payload["index"] for event in deps.event_bus.history()] == list(range(205))


def test_completion_correction_never_copies_legacy_raw_error_input():
    from redteam.orchestration.cognition import DecisionBudget
    from redteam.orchestration.engine import InvestigationEngine

    issues = CompletionIssues()
    secret = "credential-with-no-label-123456"
    issues.add(
        f"evidence '{secret}' belongs to a different run",
        code="completion_evidence_run",
        summary="The evidence must belong to this run.",
    )
    budget = DecisionBudget(3)
    feedback = InvestigationEngine._safe_errors(issues, budget)
    assert secret not in str(feedback)
    assert secret not in budget.last_failure.model_dump_json()
    assert budget.last_failure.code == "completion_evidence_run"
