import pytest

from redteam_core.models import DecisionDriverReply, DecisionStepOutcome
from redteam_core.models.base import canonical_json
from tests.integration.test_generic_step_loop import setup_engine


@pytest.mark.asyncio
async def test_reported_overrun_is_preserved_and_blocks_repair(tmp_path, monkeypatch):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])

    async def overrun(context, *, repair_feedback=None):
        driver.calls += 1
        return DecisionDriverReply(payload={}, input_tokens=9_000, output_tokens=600)

    monkeypatch.setattr(driver, "propose", overrun)
    try:
        result = await engine.run_step()
        assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
        assert driver.calls == 1
        assert runtime.operational_budget.reservations[0].actual_tokens == 9_600
        assert runtime.operational_budget.token_bound_violated
        assert runtime.usage.tokens == 9_600
        stored = orchestrator.checkpoint_store.load(
            catalog=orchestrator.catalog,
            composition_fingerprint=orchestrator.composition_fingerprint,
            journal=orchestrator.journal,
            expected_run_id=runtime.run_id,
        )
        assert stored.runtime_state.operational_budget.token_bound_violated
    finally:
        orchestrator.close()


@pytest.mark.asyncio
async def test_malformed_envelope_keeps_unknown_usage_reserved(tmp_path, monkeypatch):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, [], max_requests=1
    )

    async def malformed(context, *, repair_feedback=None):
        driver.calls += 1
        return {"payload": {}, "input_tokens": "untrusted"}

    monkeypatch.setattr(driver, "propose", malformed)
    try:
        result = await engine.run_step()
        assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
        reservation = runtime.operational_budget.reservations[0]
        assert reservation.actual_tokens is None
        assert runtime.operational_budget.charged_tokens == reservation.reserved_tokens
        assert driver.calls == 1
    finally:
        orchestrator.close()


@pytest.mark.asyncio
async def test_receipt_overhead_overflow_settles_known_failure(tmp_path, monkeypatch):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, [], max_requests=1
    )
    reply = DecisionDriverReply(payload={"data": "x" * 65_000}, input_tokens=100, output_tokens=50)
    assert len(canonical_json(reply.model_dump(mode="json")).encode()) < 65_536
    engine.count_tokens = lambda _: 1

    async def oversized(context, *, repair_feedback=None):
        driver.calls += 1
        return reply

    monkeypatch.setattr(driver, "propose", oversized)
    try:
        result = await engine.run_step()
        assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
        assert driver.calls == 1
        assert runtime.progress.active_attempt_id is None
        assert runtime.operational_budget.reservations[0].status.value == "FAILED"
        assert runtime.operational_budget.reservations[0].actual_tokens == 150
        assert not runtime.decision_responses
    finally:
        orchestrator.close()
