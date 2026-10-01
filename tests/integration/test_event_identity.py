"""Event identity checks across the real orchestration vertical slice."""

from __future__ import annotations

from redteam.app.bootstrap import run_engagement
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import RunId
from redteam.orchestration.events import HYPOTHESIS_RESOLVED, REPLAN_TRIGGERED, REPORT_PROJECTED
from tests.integration.test_end_to_end import _world


async def _run_with_events(tmp_path) -> tuple[list[EventEnvelope], RunId]:
    engagement, deps, _gateway = _world()
    events: list[EventEnvelope] = []

    async def collect(event: EventEnvelope) -> None:
        events.append(event)

    await deps.event_bus.subscribe("*", collect)
    final_state = await run_engagement(engagement, deps=deps)
    return events, final_state.run_id


async def test_action_event_chain_keeps_correlation_and_direct_causation(tmp_path) -> None:
    events, _run_id = await _run_with_events(tmp_path)
    action_events = [
        event
        for event in events
        if event.event_type
        in {
            EventType.HYPOTHESIS_CREATED,
            EventType.ACTION_PROPOSED,
            EventType.POLICY_EVALUATED,
            EventType.ACTION_APPROVED,
            EventType.TOOL_EXECUTION_STARTED,
            EventType.TOOL_EXECUTION_FINISHED,
            EventType.EVIDENCE_CREATED,
            "ObservationRecorded",
            HYPOTHESIS_RESOLVED,
            EventType.GRAPH_UPDATED,
        }
    ]

    assert action_events
    correlation_id = action_events[0].correlation_id
    assert correlation_id is not None
    assert all(event.correlation_id == correlation_id for event in action_events)
    for parent, child in zip(action_events, action_events[1:], strict=False):
        assert child.causation_id == parent.event_id


async def test_replan_triggered_keeps_run_identity_and_cites_its_cause(tmp_path) -> None:
    # ReplanTriggered is a run-level decision rather than a step in the action
    # chain, so it is checked on its own terms: the terminal decision must be
    # attributable to the event that produced it, inside the same run.
    events, run_id = await _run_with_events(tmp_path)
    triggered = [
        index for index, event in enumerate(events) if event.event_type == REPLAN_TRIGGERED
    ]

    assert triggered, "a terminal replan must be observable"
    seen = {event.event_id for event in events}
    for index in triggered:
        event = events[index]
        assert event.run_id == run_id
        assert event.engagement_id is not None
        assert event.causation_id is not None
        assert event.causation_id in seen
        assert all(earlier.event_id != event.causation_id for earlier in events[index:]), (
            "the cited cause must not appear after the effect"
        )


async def test_agent_started_cites_run_started_as_causation(tmp_path) -> None:
    events, _run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    mission_created = next(
        event for event in events if event.event_type == EventType.MISSION_CREATED
    )
    task_created = [event for event in events if event.event_type == EventType.TASK_CREATED]
    task_started = next(event for event in events if event.event_type == EventType.TASK_STARTED)
    agent_started = next(event for event in events if event.event_type == EventType.AGENT_STARTED)

    assert run_started.correlation_id == str(run_started.run_id)
    assert agent_started.correlation_id == run_started.correlation_id
    # The initial Mission/Task creation chain precedes Task dispatch.
    assert mission_created.causation_id == run_started.event_id
    assert task_created[0].causation_id == mission_created.event_id
    for parent, child in zip(task_created, task_created[1:], strict=False):
        assert child.causation_id == parent.event_id
    assert task_started.causation_id == task_created[-1].event_id
    assert agent_started.causation_id == task_started.event_id


async def test_run_finished_cites_the_last_event_and_keeps_run_correlation(tmp_path) -> None:
    events, run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    run_finished = next(event for event in events if event.event_type == EventType.RUN_FINISHED)
    mission_updated = next(
        event for event in events if event.event_type == EventType.MISSION_UPDATED
    )
    report_projected = next(event for event in events if event.event_type == REPORT_PROJECTED)

    assert run_started.correlation_id == str(run_id)
    assert run_finished.correlation_id == str(run_id)
    assert run_finished.causation_id == events[-2].event_id
    report_index = events.index(report_projected)
    assert report_projected.causation_id == events[report_index - 1].event_id
    assert events.index(mission_updated) < report_index
