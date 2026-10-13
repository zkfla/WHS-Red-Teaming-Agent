"""Dispatch-level LLM connection guard: ordinary, cohort, checkpointless.

Drives the real CLI dispatch functions with a stored binding that carries
an llm_connection_fingerprint. A changed connection must refuse BEFORE
any rebuild, transport, model call, or reconciliation; a legacy binding
without the field must still build (ordinary LiteLLM unaffected).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, load_app_config_for_run
from redteam.app.cli.commands import collect_originals as collect_command
from redteam.app.cli.commands import resume as resume_command
from redteam.core.config import LLMConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import AgentPhase
from redteam.core.ids import ActionId, ApprovalId, EngagementId, MissionId, RunId
from redteam.llm.opencode_go import llm_connection_fingerprint_for_config
from redteam.orchestration.checkpointing import CheckpointIdentity
from redteam.orchestration.component_binding import builtin_component_binding
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_activation import (
    ActivationCollectionIdentity,
    ActivationRecoveryIdentity,
)
from tests.fakes import build_test_application

POLICY_JSON = LLMRequestPolicy(mode="bounded", max_requests=5).model_dump_json()


@pytest.fixture(autouse=True)
def _explicit_fake_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        "name: " + chr(34) + "Connection Guard Test" + chr(34) + chr(10)
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


def _args(engagement_path: Path, *, run_id: str = "run_conn") -> argparse.Namespace:
    return argparse.Namespace(
        run_id=run_id,
        engagement_path=str(engagement_path),
        max_steps=None,
        log_level="WARNING",
        json=True,
    )


def _pinned_binding(*, fingerprint: str, specialist: str | None = None):
    update: dict[str, Any] = {"llm_connection_fingerprint": fingerprint}
    if specialist is not None:
        update["specialist_fingerprint"] = specialist
    return builtin_component_binding().model_copy(update=update)


class _FakeRunner:
    recovered: RuntimeState | None = None
    final: RuntimeState | None = None
    closed = False
    # resume.py reads runner.last_event_id when settling; the fake must
    # provide the attribute the real AgentLoopRunner exposes.
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


def _finished(run_id: str = "run_conn") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_conn"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=0,
    )


def _recoverable(run_id: str = "run_conn") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_conn"),
        termination_reason=TerminationReason.APPROVAL_PENDING,
        pending_approval_id=ApprovalId("appr_1"),
        pending_action_id=ActionId("action_1"),
    )


async def test_ordinary_resume_refuses_changed_llm_connection_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=None,
        component_binding=_pinned_binding(fingerprint="stored-conn-a"),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    assert await resume_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()


async def test_ordinary_resume_legacy_binding_still_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()
    _FakeRunner.recovered = _finished()
    # The resumed state must DIFFER from the recovered one: resume.py treats
    # `final == runtime` as "did not resume", and RuntimeState compares by
    # value, so two default _finished() states would trip that guard.
    _FakeRunner.final = _finished().model_copy(update={"actions_executed": 1})
    _FakeRunner.closed = False
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_conn"),
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


async def test_cohort_bootstrap_refuses_changed_llm_connection_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", POLICY_JSON)
    engagement_path = _engagement_yaml(tmp_path)
    config = load_app_config_for_run(str(engagement_path))
    policy = LLMRequestPolicy(mode="bounded", max_requests=5)
    assert config.orchestration.llm_request_policy == policy
    binding = _pinned_binding(fingerprint="stored-conn-a", specialist="spec-fp")
    launch_identity = ActivationRecoveryIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=MissionId("m-conn"),
        component_binding=binding,
        llm_request_policy=policy,
    )
    monkeypatch.setattr(
        "redteam.app.agent_profile.selected_specialists",
        lambda settings: SimpleNamespace(fingerprint="spec-fp"),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    exit_code = await resume_command._run_cohort(
        _args(engagement_path), config, [], launch_identity=launch_identity
    )
    assert exit_code == EXIT_FAILURE
    build.assert_not_awaited()


async def test_checkpointless_collect_refuses_changed_connection_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", POLICY_JSON)
    engagement_path = _engagement_yaml(tmp_path)
    policy = LLMRequestPolicy(mode="bounded", max_requests=5)
    binding = _pinned_binding(fingerprint="stored-conn-a", specialist="spec-fp")
    hint = ActivationCollectionIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=MissionId("m-conn"),
        component_binding=binding,
        llm_request_policy=policy,
        activation_digest="digest-conn",
    )
    monkeypatch.setattr(
        collect_command, "_inventory", AsyncMock(return_value=([], {"i-1"}, hint))
    )
    monkeypatch.setattr(
        collect_command, "selected_specialists",
        lambda settings: SimpleNamespace(fingerprint="spec-fp"),
    )
    build = AsyncMock()
    monkeypatch.setattr(collect_command, "build_resume_application", build)

    assert await collect_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()


GO_PROVIDER = "opencode-go"
GO_MODEL_A = "openai/conn-model-a"
GO_MODEL_B = "openai/conn-model-b"
GO_KEY_A = "OPENCODE_API_KEY_A"
GO_KEY_B = "OPENCODE_API_KEY_B"


def _go_env(
    monkeypatch: pytest.MonkeyPatch, *, model: str = GO_MODEL_A, key_var: str = GO_KEY_A,
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", GO_PROVIDER)
    monkeypatch.setenv("REDTEAM_LLM_MODEL", model)
    monkeypatch.setenv("REDTEAM_LLM_API_KEY_ENV_VAR", key_var)


def _go_fingerprint(*, model: str = GO_MODEL_A, key_var: str = GO_KEY_A) -> str:
    fingerprint = llm_connection_fingerprint_for_config(
        LLMConfig(provider=GO_PROVIDER, default_model=model, api_key_env_var=key_var)
    )
    assert fingerprint is not None
    return fingerprint


async def test_ordinary_resume_matching_go_connection_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go_env(monkeypatch)
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()
    _FakeRunner.recovered = _finished()
    # The resumed state must DIFFER from the recovered one: resume.py treats
    # `final == runtime` as "did not resume", and RuntimeState compares by
    # value, so two default _finished() states would trip that guard.
    _FakeRunner.final = _finished().model_copy(update={"actions_executed": 1})
    _FakeRunner.closed = False
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=None,
        component_binding=_pinned_binding(fingerprint=_go_fingerprint()),
    )
    build = AsyncMock(return_value=(object(), deps))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(resume_command, "settle_engagement", AsyncMock(return_value=_finished()))

    assert await resume_command.run(_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()


async def test_ordinary_resume_changed_go_model_refuses_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go_env(monkeypatch, model=GO_MODEL_B)
    engagement_path = _engagement_yaml(tmp_path)
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=None,
        component_binding=_pinned_binding(fingerprint=_go_fingerprint(model=GO_MODEL_A)),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    assert await resume_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()


async def test_ordinary_resume_changed_go_credential_ref_refuses_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go_env(monkeypatch, key_var=GO_KEY_B)
    engagement_path = _engagement_yaml(tmp_path)
    hint = CheckpointIdentity(
        engagement_id=EngagementId("eng_conn"),
        mission_id=None,
        component_binding=_pinned_binding(fingerprint=_go_fingerprint(key_var=GO_KEY_A)),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    assert await resume_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()
