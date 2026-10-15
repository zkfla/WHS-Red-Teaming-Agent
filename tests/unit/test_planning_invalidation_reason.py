"""Planning invalidation events keep the validator's bounded owner cause."""

from types import SimpleNamespace

import pytest

from redteam.orchestration.coordination import PlanningInputChangeCode, PlanningInputsChanged
from redteam.orchestration.hypothesis_planner import (
    PLANNING_INVALIDATED,
    _finalize_hypothesis_pass,
)


class _Tx:
    def __init__(self, error):
        self.error = error
        self.events = []

    def check_planning_inputs(self, *args):
        raise self.error

    def queue(self, envelope):
        self.events.append(envelope)


class _State:
    run_id = "run-test"
    engagement_id = "engagement-test"

    def __init__(self, error):
        self.tx = _Tx(error)

    async def transaction(self, callback):
        return callback(self.tx)


@pytest.mark.parametrize("cause", list(PlanningInputChangeCode))
def test_planning_inputs_changed_retains_only_owner_defined_causes(cause):
    error = PlanningInputsChanged(cause.value)

    assert error.code == cause.value
    assert str(error) == cause.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_code", "expected_cause"),
    [
        ("planner_context_edge_changed", "planner_context_edge_changed"),
        ("private-secret-value", "planning_inputs_changed"),
        ("plausible_but_unknown_code", "planning_inputs_changed"),
    ],
)
async def test_invalidation_event_keeps_generic_reason_and_safe_cause(raw_code, expected_cause):
    error = PlanningInputsChanged(raw_code)
    state = _State(error)
    deps = SimpleNamespace(central_state=state, event_bus=SimpleNamespace(flush=_noop))
    snapshot = SimpleNamespace(mission=SimpleNamespace(revision=1), binding="binding-1")

    result = await _finalize_hypothesis_pass(
        deps, snapshot, budget=None, identity="pass-test", proposal=None, judgment={},
        graph_read={}, task_read={}, targets=set(), principals=set(), max_tasks=3,
    )

    assert result == []
    assert len(state.tx.events) == 1
    envelope = state.tx.events[0]
    assert envelope.event_type == PLANNING_INVALIDATED
    assert envelope.payload == {
        "pass_id": "pass-test",
        "reason_code": "related_planning_inputs_changed",
        "cause_code": expected_cause,
    }
    if raw_code == "private-secret-value":
        assert raw_code not in str(envelope.payload)


async def _noop():
    return None
