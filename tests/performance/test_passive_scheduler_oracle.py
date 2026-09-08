"""Independent small batch model: no production scheduler helpers are used for expected results."""

import itertools

import pytest

from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from tests.integration.test_durable_resolver_dispatch import setup as durable_setup

setup = durable_setup


def oracle(groups):
    keys = sorted(set(groups))
    return {
        "calls": len(keys),
        "waiters": tuple(
            (key, tuple(sorted(i for i, value in enumerate(groups) if value == key)))
            for key in keys
        ),
    }


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("setup", [{"op_requests": 3, "max_calls": 3}], indirect=True)
async def test_grouping_matches_independent_oracle_for_input_orders(setup, order):
    dispatcher, resolver, req, _ = setup
    rows = (("a", req.requirement_id), ("b", req.requirement_id), ("c", req.requirement_id))
    ordered = tuple(rows[index] for index in order)
    expected = oracle([req.requirement_id] * 3)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    batch = scheduler.admit(batch_id="one", requests=ordered)
    assert len(batch.items) == expected["calls"]
    assert batch.items[0].waiter_ids == ("a", "b", "c")
    await scheduler.run("one")
    scheduler.commit("one", expected_revision=0)
    assert resolver.calls == expected["calls"]
    assert len(dispatcher.session.investigation.observations) == 1


@pytest.mark.parametrize("setup", [{"op_requests": 1}], indirect=True)
async def test_completion_tape_does_not_change_canonical_result(setup):
    dispatcher, resolver, req, _ = setup
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("x", req.requirement_id), ("y", req.requirement_id)))
    await scheduler.run("one")
    result = scheduler.commit("one", expected_revision=0)
    projection = (
        tuple(sorted(dispatcher.session.investigation.observations)),
        tuple(sorted(dispatcher.session.knowledge.slots)),
        result.revision,
    )
    assert projection[0] == (dispatcher.runtime.resolution_receipts[0].observation.observation_id,)
    assert projection[1] == (req.requirement_id,) and projection[2] == 1
