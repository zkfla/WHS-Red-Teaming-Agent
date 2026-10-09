"""Resume cohort helpers: selection, coherence, accounting totals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from redteam.app.cli.commands import resume as resume_cmd
from redteam.app.cli.commands.resume import (
    _check_cohort_coherence,
    _cohort_rehydrate_totals,
    _CohortRefused,
    _select_cohort_identities,
)
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, RunId
from redteam.orchestration.checkpointing import CheckpointThreadIdentity
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _identity(instance: str | None, *, engagement: str = "eng-1", mission: str | None = "m-1"):
    thread = "run-1" if instance is None else f"run-1:instance:{instance}"
    return CheckpointThreadIdentity(
        thread_id=thread,
        run_id="run-1",
        instance_id=instance,
        engagement_id=EngagementId(engagement),
        mission_id=mission,
        component_binding=None,
    )


def test_single_instance_thread_is_the_non_legacy_default() -> None:
    # Fresh runs allocate an instance id: one per-instance thread with no
    # flag resumes as the cohort (never assumed to be the legacy thread).
    found = [_identity("only")]
    assert _select_cohort_identities(found, None) == found


def test_legacy_singleton_selected_without_flag() -> None:
    found = [_identity(None)]
    assert _select_cohort_identities(found, None) == found


def test_mixed_legacy_and_instance_refused_without_authority() -> None:
    with pytest.raises(_CohortRefused, match="mixed legacy singleton"):
        _select_cohort_identities([_identity(None), _identity("a")], None)


def test_partial_selects_exact_instance() -> None:
    found = [_identity("a"), _identity("b")]
    assert _select_cohort_identities(found, "b") == [found[1]]
    with pytest.raises(_CohortRefused, match="no checkpoint thread"):
        _select_cohort_identities(found, "zzz")


def test_coherence_accepts_equal_and_refuses_mixed() -> None:
    engagement, mission, binding = _check_cohort_coherence([_identity("a"), _identity("b")])
    assert (str(engagement), mission, binding) == ("eng-1", "m-1", None)
    with pytest.raises(_CohortRefused, match="engagement"):
        _check_cohort_coherence([_identity("a"), _identity("b", engagement="eng-2")])
    with pytest.raises(_CohortRefused, match="mission"):
        _check_cohort_coherence([_identity("a"), _identity("b", mission="m-2")])
    with pytest.raises(_CohortRefused, match="no checkpoint identities"):
        _check_cohort_coherence([])


def _counted(actions: int, retries: int, *, started: datetime | None) -> RuntimeState:
    return RuntimeState(
        run_id=RunId("run-1"),
        engagement_id=EngagementId("eng-1"),
        actions_executed=actions,
        retry_tool_calls=retries,
        started_at=started,
    )


def test_totals_count_full_cohort_not_selection() -> None:
    # Partial resume still rehydrates from every cohort member: the held
    # instance's projected counters join the healthy one's.
    accounted = [
        _counted(3, 1, started=NOW - timedelta(seconds=90)),
        _counted(5, 2, started=NOW - timedelta(seconds=30)),
    ]
    engagement, actions, tool_calls, elapsed = _cohort_rehydrate_totals(accounted, now=NOW)
    assert str(engagement) == "eng-1"
    assert actions == 8
    assert tool_calls == 11
    assert elapsed == 90.0


def test_totals_without_start_times() -> None:
    accounted = [_counted(1, 0, started=None)]
    _engagement, actions, tool_calls, elapsed = _cohort_rehydrate_totals(accounted, now=NOW)
    assert (actions, tool_calls, elapsed) == (1, 1, None)


class _StubRunner:
    """Owned-runner double: canned recover, graceful resume entries."""

    def __init__(self, recover=None) -> None:
        self._recover = recover
        self.resumed: list[str] = []

    async def recover_reconciling(self, run_id, *, instance_id=None):
        assert self._recover is not None
        return self._recover

    async def resume_recovered(self, runtime):
        self.resumed.append("recovered")
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    async def resume_reconciled(self, runtime):
        self.resumed.append("reconciled")
        return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

    def request_stop(self) -> bool:
        return False


class _StubControl:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def rehydrate(self, engagement_id, *, actions_consumed, tool_calls_consumed,
                          elapsed_duration_seconds):
        self.calls.append({
            "engagement_id": engagement_id,
            "actions_consumed": actions_consumed,
            "tool_calls_consumed": tool_calls_consumed,
            "elapsed": elapsed_duration_seconds,
        })


def _guarded_deps(control):
    from types import SimpleNamespace

    return SimpleNamespace(control_plane=control)


def _guarded_driver(monkeypatch, runners):
    driver = InstanceDriver(
        None,  # type: ignore[arg-type]
        settings=OrchestrationSettings(checkpointing=False),
    )
    driver.runner_for = lambda runtime: runners[str(runtime.instance_id)]  # type: ignore[method-assign]
    return driver


def _guarded_args():
    from types import SimpleNamespace

    return SimpleNamespace(run_id="run-1")


async def test_all_held_reports_without_start(monkeypatch, capsys) -> None:
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

    control = _StubControl()
    deps = _guarded_deps(control)
    rt = _counted(4, 2, started=NOW - timedelta(seconds=60))
    rt = rt.model_copy(update={"instance_id": "a"})
    held_rec = ExecutionReconciliation(runtime=rt, pending_invocations=["inv-x"])
    assert not held_rec.ready
    driver = _guarded_driver(monkeypatch, {"a": _StubRunner((rt, held_rec))})
    await driver.open()
    drive_calls: list[str] = []

    def fail_drive(items):
        drive_calls.append("drive")
        raise AssertionError("empty drive must not start")

    monkeypatch.setattr(driver, "drive_resumed", fail_drive)
    try:
        code = await resume_cmd._drive_cohort_guarded(
            driver, _guarded_args(), deps, [_identity("a")], [_identity("a")]
        )
    finally:
        await driver.close()
    assert drive_calls == []
    # Held receipts still project: the aggregate counts the held member.
    assert [(c["actions_consumed"], c["tool_calls_consumed"]) for c in control.calls] == [(4, 6)]
    assert "held" in capsys.readouterr().err
    from redteam.app.cli._shared import EXIT_OK

    assert code == EXIT_OK


async def test_partial_unselected_defers_global_settle(monkeypatch, capsys) -> None:
    control = _StubControl()
    deps = _guarded_deps(control)
    rt_a = _counted(3, 1, started=NOW - timedelta(seconds=90))
    rt_a = rt_a.model_copy(update={"instance_id": "a"})
    rt_b = _counted(5, 2, started=NOW - timedelta(seconds=30))
    rt_b = rt_b.model_copy(update={"instance_id": "b"})
    runner_a = _StubRunner((rt_a, None))
    runner_b = _StubRunner((rt_b, None))
    driver = _guarded_driver(monkeypatch, {"a": runner_a, "b": runner_b})
    await driver.open()
    settled: list[str] = []

    async def spy_settle(final, _deps):
        settled.append(str(final.instance_id))
        return final

    monkeypatch.setattr(resume_cmd, "settle_engagement", spy_settle)
    try:
        code = await resume_cmd._drive_cohort_guarded(
            driver, _guarded_args(), deps, [_identity("a")], [_identity("a"), _identity("b")]
        )
    finally:
        await driver.close()
    # Partial counts the FULL cohort (a and b) in rehydrate...
    assert [(c["actions_consumed"], c["tool_calls_consumed"]) for c in control.calls] == [(8, 11)]
    # ...but only the selection drove, so no conclusive global settle.
    assert settled == []
    assert "deferred" in capsys.readouterr().err
    assert runner_a.resumed == ["recovered"]
    assert runner_b.resumed == []
    from redteam.app.cli._shared import EXIT_OK

    assert code == EXIT_OK
