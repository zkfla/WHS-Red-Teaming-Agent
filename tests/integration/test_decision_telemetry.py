import pytest

from redteam_core.diagnostics.performance import PerformanceStage, observe_performance
from redteam_core.models import DecisionAction
from tests.integration.test_generic_step_loop import proposal, setup_engine


@pytest.mark.asyncio
async def test_actual_step_telemetry_is_numeric_opt_in_and_truth_neutral(tmp_path):
    def broken(_event):
        raise RuntimeError("observer unavailable")

    events = []
    outputs = []
    for index, observer in enumerate((None, events.append, broken)):
        directory = tmp_path / str(index)
        directory.mkdir()
        engine, session, runtime, _, driver, orchestrator = setup_engine(directory, [])
        try:
            driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="assess"))
            with observe_performance(observer):
                result = await engine.run_step()
            outputs.append((result.model_dump(mode="json"), session.model_dump(mode="json")))
        finally:
            orchestrator.close()
    assert outputs[0] == outputs[1] == outputs[2]
    decisions = [row for row in events if row.stage is PerformanceStage.DECISION]
    assert len(decisions) == 1
    assert decisions[0].outcome == "OK"
    assert decisions[0].input_items == 1
    assert decisions[0].elapsed_ns >= 0
    assert decisions[0].input_bytes is None
    assert "must-never-enter-context" not in repr(events)
