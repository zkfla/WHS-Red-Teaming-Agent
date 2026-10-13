"""Dispatch-level execution-environment fingerprint forwarding.

Drives the real CLI dispatch entry points with a stored binding that carries
an execution_environment_fingerprint, and asserts the mocked
build_resume_application received exactly that value. A callsite that stops
forwarding produces a call without the kwarg and fails here. A legacy stored
binding without the field must still build, omitting the kwarg key entirely.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from redteam.app.cli._shared import EXIT_OK, load_app_config_for_run
from redteam.app.cli.commands import collect_originals as collect_command
from redteam.app.cli.commands import resume as resume_command
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import AgentPhase
from redteam.core.ids import ActionId, ApprovalId, EngagementId, MissionId, RunId
from redteam.orchestration.checkpointing import CheckpointIdentity, CheckpointThreadIdentity
from redteam.orchestration.component_binding import (
    RuntimeComponentBinding,
    builtin_component_binding,
)
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_activation import ActivationCollectionIdentity
from tests.fakes import build_test_application

EXEC_FP = "ab" * 32
POLICY_JSON = LLMRequestPolicy(mode="bounded", max_requests=5).model_dump_json()


@pytest.fixture(autouse=True)
def _explicit_fake_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        "name: " + chr(34) + "Execution Environment Test" + chr(34) + chr(10)
        + "scope:" + chr(10)
        + "  allowed_hosts: [" + chr(34) + "a.local" + chr(34) + "]" + chr(10)
        + "execution_budget:" + chr(10)
        + "  max_actions: 1" + chr(10)
        + "rate_limit_policy:" + chr(10)
        + "  max_actions_per_minute: 10" + chr(10)
        + "approval_policy:" + chr(10)
        + "  require_approval_for: []" + chr(10),
        encoding="utf-8",
    )
    return path


def _args(engagement_path: Path, *, run_id: str = "run_exec") -> argparse.Namespace:
    return argparse.Namespace(
        run_id=run_id,
        engagement_path=str(engagement_path),
        max_steps=None,
        log_level="WARNING",
        json=True,
    )


def _collect_args(engagement_path: Path, *, run_id: str = "run_exec") -> argparse.Namespace:
    return argparse.Namespace(
        run_id=run_id,
        engagement_path=str(engagement_path),
        json=False,
    )


def _exec_binding(*, specialist: str | None = None) -> RuntimeComponentBinding:
    update: dict[str, Any] = {"execution_environment_fingerprint": EXEC_FP}
    if specialist is not None:
        update["specialist_fingerprint"] = specialist
    return builtin_component_binding().model_copy(update=update)


class _FakeRunner:
    recovered: RuntimeState | None = None
    final: RuntimeState | None = None
    closed = False
    last_event_id: int | None = None

    def __init__(self, deps: Any, *, settings: Any = None) -> None:
        self.deps = deps

    async def recover_runtime(self, run_id: str) -> RuntimeState | None:
        return type(self).recovered

    async def resume_recovered(self, runtime: RuntimeState) -> RuntimeState:
        final = type(self).final
        assert final is not None
        return final

    async def aclose(self) -> None:
        type(self).closed = True


class _FakeCollectDriver:
    def __init__(self, deps: Any, *, settings: Any = None) -> None:
        self.deps = deps

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def own_run(self, run_id: str) -> None:
        return None

    async def collect_checkpointless_originals(
        self, run_id: str, *, checkpoint_instances: set[str],
    ) -> list[Any]:
        original = SimpleNamespace(
            state="known",
            invocation_id="inv-1",
            reason_code="rc",
            original_receipt_id="rec-1",
        )
        return [SimpleNamespace(instance_id="i-1", originals=[original])]


def _finished(run_id: str = "run_exec") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_exec"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=0,
    )


def _recoverable(run_id: str = "run_exec") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_exec"),
        termination_reason=TerminationReason.APPROVAL_PENDING,
        pending_approval_id=ApprovalId("appr_1"),
        pending_action_id=ActionId("action_1"),
    )


def _prime_ordinary_success() -> None:
    _FakeRunner.recovered = _recoverable()
    # The resumed state must DIFFER from the recovered one: resume.py treats
    # "final == runtime" as "did not resume", and RuntimeState compares by
    # value, so two default states would trip that guard.
    _FakeRunner.final = _recoverable().model_copy(update={"actions_executed": 1})
    _FakeRunner.closed = False


async def test_ordinary_resume_forwards_stored_execution_environment_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()
    _prime_ordinary_success()
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_exec"),
        mission_id=None,
        component_binding=_exec_binding(),
    )
    build = AsyncMock(return_value=(object(), deps))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(resume_command, "settle_engagement", AsyncMock(return_value=_finished()))

    assert await resume_command.run(_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert build.await_args.kwargs["execution_environment_fingerprint"] == EXEC_FP


async def test_ordinary_resume_legacy_binding_omits_execution_environment_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()
    _prime_ordinary_success()
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_exec"),
        mission_id=None,
        component_binding=None,
    )
    build = AsyncMock(return_value=(object(), deps))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(resume_command, "settle_engagement", AsyncMock(return_value=_finished()))

    assert await resume_command.run(_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert "execution_environment_fingerprint" not in build.await_args.kwargs


def _cohort_identities(binding: Any) -> list[CheckpointThreadIdentity]:
    return [
        CheckpointThreadIdentity(
            thread_id=f"run_exec:instance:{name}",
            run_id="run_exec",
            instance_id=name,
            engagement_id=EngagementId("eng_exec"),
            mission_id="m-exec",
            component_binding=binding,
        )
        for name in ("i-1", "i-2")
    ]


async def test_cohort_resume_forwards_stored_execution_environment_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    config = load_app_config_for_run(str(engagement_path))
    build = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "_drive_cohort", AsyncMock(return_value=EXIT_OK))

    exit_code = await resume_command._run_cohort(
        _args(engagement_path), config, _cohort_identities(_exec_binding()),
    )
    assert exit_code == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert build.await_args.kwargs["execution_environment_fingerprint"] == EXEC_FP


async def test_cohort_resume_legacy_binding_omits_execution_environment_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    config = load_app_config_for_run(str(engagement_path))
    build = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "_drive_cohort", AsyncMock(return_value=EXIT_OK))

    exit_code = await resume_command._run_cohort(
        _args(engagement_path), config, _cohort_identities(None),
    )
    assert exit_code == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert "execution_environment_fingerprint" not in build.await_args.kwargs


def _collect_hint(binding: Any) -> ActivationCollectionIdentity:
    policy = LLMRequestPolicy(mode="bounded", max_requests=5)
    return ActivationCollectionIdentity(
        engagement_id=EngagementId("eng_exec"),
        mission_id=MissionId("m-exec"),
        component_binding=binding,
        llm_request_policy=policy,
        activation_digest="digest-exec",
    )


async def test_checkpointless_collect_forwards_stored_execution_environment_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", POLICY_JSON)
    engagement_path = _engagement_yaml(tmp_path)
    hint = _collect_hint(_exec_binding(specialist="spec-fp"))
    monkeypatch.setattr(
        collect_command, "_inventory", AsyncMock(return_value=([], {"i-1"}, hint)),
    )
    monkeypatch.setattr(
        collect_command, "selected_specialists",
        lambda settings: SimpleNamespace(fingerprint="spec-fp"),
    )
    monkeypatch.setattr(collect_command, "InstanceDriver", _FakeCollectDriver)
    build = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr(collect_command, "build_resume_application", build)

    assert await collect_command.run(_collect_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert build.await_args.kwargs["execution_environment_fingerprint"] == EXEC_FP


async def test_checkpointless_collect_legacy_binding_omits_execution_environment_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", POLICY_JSON)
    engagement_path = _engagement_yaml(tmp_path)
    legacy = builtin_component_binding().model_copy(
        update={"specialist_fingerprint": "spec-fp"},
    )
    hint = _collect_hint(legacy)
    monkeypatch.setattr(
        collect_command, "_inventory", AsyncMock(return_value=([], {"i-1"}, hint)),
    )
    monkeypatch.setattr(
        collect_command, "selected_specialists",
        lambda settings: SimpleNamespace(fingerprint="spec-fp"),
    )
    monkeypatch.setattr(collect_command, "InstanceDriver", _FakeCollectDriver)
    build = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr(collect_command, "build_resume_application", build)

    assert await collect_command.run(_collect_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert "execution_environment_fingerprint" not in build.await_args.kwargs


def test_execution_environment_fingerprint_rejects_wrong_shape() -> None:
    with pytest.raises(ValidationError):
        RuntimeComponentBinding(execution_environment_fingerprint="not-hex")
    assert "execution_environment_fingerprint" not in builtin_component_binding().model_dump()
