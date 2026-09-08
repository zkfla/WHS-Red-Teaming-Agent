import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from redteam_core.models import DecisionAction, DecisionStepOutcome, RuntimeBudgetLimits
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.step_loop import GenericStepEngine
from tests.helpers import catalog
from tests.integration.test_generic_step_loop import ScriptedDriver, proposal, setup_engine


def crash_worker(directory: str, cut: str):
    engine, session, _runtime, _candidate, driver, _orchestrator = setup_engine(
        Path(directory), [], max_requests=1
    )
    driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="restart"))
    if cut == "response":

        def die_after_response(_proposal):
            os._exit(73)

        engine._validate_proposal = die_after_response
    else:

        async def die_after_dispatch(context, *, repair_feedback=None):
            os._exit(73)

        driver.propose = die_after_dispatch
    asyncio.run(engine.run_step())
    raise AssertionError("crash cut was not reached")


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["response", "dispatch"])
async def test_actual_process_restart_never_repeats_driver_call(tmp_path, cut):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.integration.test_decision_process_restart import crash_worker; "
            "import sys; crash_worker(sys.argv[1], sys.argv[2])",
            str(tmp_path),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 73, child.stdout + child.stderr
    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    replacement = ScriptedDriver([])
    manager = RuntimeOrchestrator(
        cat,
        build_runtime_composition_fingerprint(
            catalog=cat, decision_driver_hash=replacement.descriptor.descriptor_hash
        ),
        clock,
        EventJournal(tmp_path / "events.jsonl", run_id="step:one", clock=clock),
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=1, max_tokens=200_000),
    )
    try:
        resumed = manager.resume(expected_run_id="step:one")
        result = await GenericStepEngine(
            session=resumed.session,
            runtime=resumed.runtime_state,
            orchestrator=manager,
            driver=replacement,
            active_candidate_ids=tuple(resumed.session.candidate_state.candidates),
            count_tokens=lambda content: len(content.encode("utf-8")) // 4,
        ).run_step()
        assert replacement.calls == 0
        assert resumed.runtime_state.usage.requests == 1
        if cut == "response":
            assert result.outcome is DecisionStepOutcome.FINISH_REQUESTED
            assert len(resumed.runtime_state.completion_assessments) == 1
        else:
            assert result.outcome is DecisionStepOutcome.WAITING
            assert (
                resumed.runtime_state.operational_budget.reservations[0].status.value == "UNCERTAIN"
            )
    finally:
        manager.close()
