"""Event identity checks across the real orchestration vertical slice.

The producer-gap checks are intentionally marked xfail until orchestration and
bootstrap owners thread the initial/terminal lifecycle context through state.
They keep the required identity rules visible without changing producer code
from the Event Bus team.
"""

from __future__ import annotations

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import RunId
from tests.integration.test_end_to_end import _engagement


async def _run_with_events(tmp_path) -> tuple[list[EventEnvelope], RunId]:
    engagement = _engagement()
    deps = await build_runtime_deps(engagement, evidence_dir=tmp_path)
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
            EventType.GRAPH_UPDATED,
        }
    ]

    assert action_events
    correlation_id = action_events[0].correlation_id
    assert correlation_id is not None
    assert all(event.correlation_id == correlation_id for event in action_events)
    for parent, child in zip(action_events, action_events[1:], strict=False):
        assert child.causation_id == parent.event_id


@pytest.mark.xfail(
    strict=False,
    reason="bootstrap must seed RunStarted event identity into orchestration state",
)
async def test_agent_started_cites_run_started_as_causation(tmp_path) -> None:
    events, _run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    agent_started = next(event for event in events if event.event_type == EventType.AGENT_STARTED)

    assert run_started.correlation_id == str(run_started.run_id)
    assert agent_started.correlation_id == run_started.correlation_id
    assert agent_started.causation_id == run_started.event_id


@pytest.mark.xfail(
    strict=False,
    reason="bootstrap must publish RunFinished with the terminal event context",
)
async def test_run_finished_cites_the_last_event_and_keeps_run_correlation(tmp_path) -> None:
    events, run_id = await _run_with_events(tmp_path)
    run_started = next(event for event in events if event.event_type == EventType.RUN_STARTED)
    run_finished = next(event for event in events if event.event_type == EventType.RUN_FINISHED)

    assert run_started.correlation_id == str(run_id)
    assert run_finished.correlation_id == str(run_id)
    assert run_finished.causation_id == events[-2].event_id
