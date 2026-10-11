"""CLI resume cohort wiring: failures refuse BEFORE any build/transport.

Provider-level tests cannot prove the CLI adapts errors, so these drive
the real ``_run`` with a stubbed enumeration/build seam: a throwing
enumeration must fail without building, and a missing ``--instance`` must
fail without falling back to the legacy path.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from types import SimpleNamespace

from redteam.app.cli._shared import EXIT_FAILURE
from redteam.app.cli.commands import resume as resume_cmd
from redteam.core.enums import AgentPhase
from redteam.core.ids import AgentInstanceId, EngagementId, MissionId, RunId
from redteam.orchestration.checkpointing import CheckpointThreadIdentity
from redteam.orchestration.coordination import CLOSED
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason

ENGAGEMENT_YAML = """
name: cli cohort test
description: enumeration failure must refuse before build
scope:
  allowed_hosts: [127.0.0.1]
execution_budget:
  max_actions: 5
  max_tool_calls: 5
  max_duration_seconds: 60
rate_limit_policy:
  max_actions_per_minute: 10
  max_concurrent_actions: 1
approval_policy:
  auto_approve: true
"""


def _args(engagement_path: str, *, instance: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-cli",
        engagement_path=engagement_path,
        max_steps=None,
        log_level="WARNING",
        json=False,
        instance=instance,
    )


def _identity(instance: str | None) -> CheckpointThreadIdentity:
    thread = "run-cli" if instance is None else f"run-cli:instance:{instance}"
    return CheckpointThreadIdentity(
        thread_id=thread,
        run_id="run-cli",
        instance_id=instance,
        engagement_id=EngagementId("eng-cli"),
        mission_id="mission-cli",
        component_binding=None,
    )


async def _run_cli(monkeypatch, args, *, enumerated=None, enumerate_error=None):
    built: list[str] = []

    async def fake_enumerate(**kwargs):
        if enumerate_error is not None:
            raise enumerate_error
        return enumerated

    async def spy_build(*inner_args, **kwargs):
        built.append("build")
        raise AssertionError("app build must not run after refusal")

    monkeypatch.setattr(resume_cmd, "list_checkpoint_threads", fake_enumerate)
    monkeypatch.setattr(resume_cmd, "build_resume_application", spy_build)
    async with AsyncExitStack() as stack:
        code = await resume_cmd._run(args, mcp_stack=stack)
    return code, built


async def test_enumeration_failure_refuses_before_build(tmp_path, monkeypatch, capsys) -> None:
    engagement = tmp_path / "engagement.yaml"
    engagement.write_text(ENGAGEMENT_YAML)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "test-provider")
    code, built = await _run_cli(
        monkeypatch, _args(str(engagement)),
        enumerate_error=sqlite3.OperationalError("locked"),
    )
    assert code == EXIT_FAILURE
    assert built == []
    assert "could not enumerate" in capsys.readouterr().err


async def test_partial_missing_instance_fails_without_fallback(
    tmp_path, monkeypatch, capsys
) -> None:
    engagement = tmp_path / "engagement.yaml"
    engagement.write_text(ENGAGEMENT_YAML)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "test-provider")
    code, built = await _run_cli(
        monkeypatch, _args(str(engagement), instance="zzz"),
        enumerated=[_identity("a")],
    )
    assert code == EXIT_FAILURE
    assert built == []
    assert "no checkpoint thread" in capsys.readouterr().err


def _base_runtime(instance: str) -> RuntimeState:
    return RuntimeState(
        run_id=RunId("run-cli"),
        engagement_id=EngagementId("eng-cli"),
        instance_id=AgentInstanceId(instance),
        mission_id=MissionId("mission-cli"),
        phase=AgentPhase.OBSERVE,
    )


async def _finish(runtime: RuntimeState) -> RuntimeState:
    return runtime.model_copy(
        update={
            "phase": AgentPhase.FINISHED,
            "termination_reason": TerminationReason.GOAL_ACHIEVED,
        }
    )


async def _raise_failure(runtime: RuntimeState) -> RuntimeState:
    raise RuntimeError("tool exploded")


async def _raise_cancelled(runtime: RuntimeState) -> RuntimeState:
    raise asyncio.CancelledError


class _ResumeRunner:
    """Owned-runner double: canned recovery, scripted resume outcome."""

    def __init__(self, base: RuntimeState, outcome) -> None:
        self._base = base
        self._outcome = outcome

    async def recover_reconciling(self, run_id, *, instance_id=None):
        return self._base, None

    async def resume_recovered(self, runtime):
        return await self._outcome(runtime)

    def request_stop(self) -> bool:
        return False


class _ControlPlane:
    async def rehydrate(self, engagement_id, **kwargs):
        return None


class _TaskGraphStore:
    def __init__(self, *, terminal: bool) -> None:
        self._terminal = terminal

    async def get_mission(self, mission_id):
        if not self._terminal:
            return None
        return SimpleNamespace(status=next(iter(CLOSED)))


async def _resume_cohort_cli(
    monkeypatch, tmp_path, outcomes, *, terminal, settle_raises_for=()
):
    """Drive the real CLI `_run` over a two-instance cohort.

    Only enumeration, bootstrap, the per-instance runners, and settlement are
    replaced; the driver, cohort routing, and exit-code logic are the real code.
    """
    engagement = tmp_path / "engagement.yaml"
    engagement.write_text(ENGAGEMENT_YAML)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "test-provider")
    identities = [_identity(name) for name in outcomes]
    runners = {
        name: _ResumeRunner(_base_runtime(name), outcome)
        for name, outcome in outcomes.items()
    }
    deps = SimpleNamespace(
        control_plane=_ControlPlane(),
        task_graph_store=_TaskGraphStore(terminal=terminal),
    )

    def make_driver(driver_deps, *, settings=None, checkpointer=None):
        driver = InstanceDriver(
            driver_deps, settings=OrchestrationSettings(checkpointing=False)
        )
        driver.runner_for = lambda runtime: runners[str(runtime.instance_id)]
        return driver

    async def fake_enumerate(**kwargs):
        return identities

    async def fake_build(*args, **kwargs):
        return None, deps

    settled: list[str] = []
    finished: list[RuntimeState] = []

    async def spy_settle(final, _deps):
        settled.append(str(final.instance_id))
        if str(final.instance_id) in settle_raises_for:
            raise RuntimeError("settle exploded")
        return final

    monkeypatch.setattr(resume_cmd, "list_checkpoint_threads", fake_enumerate)
    monkeypatch.setattr(resume_cmd, "build_resume_application", fake_build)
    monkeypatch.setattr(resume_cmd, "InstanceDriver", make_driver)
    monkeypatch.setattr(resume_cmd, "settle_engagement", spy_settle)
    async with AsyncExitStack() as stack:
        code = await resume_cmd._run(
            _args(str(engagement)),
            mcp_stack=stack,
            on_finished=finished.append,
        )
    return code, settled, [str(item.instance_id) for item in finished], finished


async def test_failed_base_run_fails_cohort_and_sibling_still_settles(
    tmp_path, monkeypatch, capsys
) -> None:
    # Terminal mission: the failed run must still be excluded from settle.
    code, settled, finished_ids, finished = await _resume_cohort_cli(
        monkeypatch, tmp_path,
        {"a": _raise_failure, "b": _finish},
        terminal=True,
    )
    assert code == EXIT_FAILURE
    assert settled == ["b"]
    assert finished_ids == ["b"]
    assert finished[0].phase is AgentPhase.FINISHED
    err = capsys.readouterr().err
    assert "settle skipped (run failed)" in err


async def test_terminated_base_run_fails_cohort_and_blocks_closing_settle(
    tmp_path, monkeypatch, capsys
) -> None:
    # Open mission: a terminated sibling is unsettled, so the closing settle is
    # deferred for the healthy run, which is still reported via on_finished.
    code, settled, finished_ids, _finished = await _resume_cohort_cli(
        monkeypatch, tmp_path,
        {"a": _raise_cancelled, "b": _finish},
        terminal=False,
    )
    assert code == EXIT_FAILURE
    assert settled == []
    assert finished_ids == ["b"]
    err = capsys.readouterr().err
    assert "WARNING: instance 'a' terminated" in err
    assert "settle deferred (sibling unsettled)" in err


async def test_settle_exception_fails_cohort_and_sibling_still_settles(
    tmp_path, monkeypatch, capsys
) -> None:
    code, settled, finished_ids, _finished = await _resume_cohort_cli(
        monkeypatch, tmp_path,
        {"a": _finish, "b": _finish},
        terminal=False,
        settle_raises_for={"a"},
    )
    assert code == EXIT_FAILURE
    assert sorted(settled) == ["a", "b"]
    # A settle that raised is not a successful settlement: no callback for it.
    assert finished_ids == ["b"]
    assert "ERROR: instance 'a' settle failed: RuntimeError" in capsys.readouterr().err
