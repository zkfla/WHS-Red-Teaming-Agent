"""``redteam resume <run_id> <engagement.yaml>`` -- CLI wiring.

Parses/registers the command, reads the run's original engagement id from its
durable checkpoint, reaches the canonical ``build_application`` (never a
CLI-only runtime), and fails closed whenever the run cannot be recovered.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from redteam.app.cli import _build_parser
from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK
from redteam.app.cli.commands import resume as resume_command
from redteam.capability.settings import MCPConfigurationError
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.core.ids import ActionId, ApprovalId, EngagementId, MissionId, RunId
from redteam.orchestration.checkpointing import (
    CheckpointIdentity,
    StateCodecRegistry,
    build_checkpointer,
    open_checkpointer,
    open_identity_reader,
    read_checkpoint_identity,
)
from redteam.orchestration.component_binding import (
    BUILTIN_COMPONENT_IDS,
    ComponentProfile,
    RuntimeComponentBinding,
)
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.operator_intents import InMemoryOperatorIntentStore
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from tests.fakes import build_test_application

_CONSTRUCTED: list[str] = []


class _IdentityProbe(BaseModel):
    label: str = "probe"

    def __init__(self, **data: Any) -> None:
        _CONSTRUCTED.append("probe")
        super().__init__(**data)


def _identity(
    engagement_id: str = "eng_recovered",
    mission_id: str | None = None,
    binding: RuntimeComponentBinding | None = None,
) -> CheckpointIdentity:
    return CheckpointIdentity(
        engagement_id=EngagementId(engagement_id),
        mission_id=mission_id,
        component_binding=binding,
    )


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        """
name: "Resume Test"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _explicit_fake_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real config loader needs an explicit provider."""
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")


def _args(engagement_path: Path, *, run_id: str = "run_resume") -> argparse.Namespace:
    return argparse.Namespace(
        run_id=run_id,
        engagement_path=str(engagement_path),
        max_steps=None,
        log_level="WARNING",
        json=True,
    )


class _FakeRunner:
    recovered: RuntimeState | None = None
    final: RuntimeState | None = None
    resumed_with: RuntimeState | None = None
    closed = False

    def __init__(self, deps, *, settings=None) -> None:  # noqa: ANN001
        self.deps = deps
        self.settings = settings
        self.last_event_id = None

    async def recover_runtime(self, run_id: str) -> RuntimeState | None:
        return type(self).recovered

    async def resume_recovered(self, runtime: RuntimeState) -> RuntimeState:
        type(self).resumed_with = runtime
        final = type(self).final
        assert final is not None
        return final

    async def aclose(self) -> None:
        type(self).closed = True


def _finished(run_id: str = "run_resume") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_recovered"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=0,
    )


def test_resume_is_registered_as_a_cli_command() -> None:
    parser = _build_parser()
    args = parser.parse_args(["resume", "run_1", "engagement.yaml"])

    assert args.handler is resume_command.run
    assert args.run_id == "run_1"
    assert args.engagement_path == "engagement.yaml"


async def test_resume_uses_canonical_bootstrap_and_the_recovered_engagement_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()

    recovered = RuntimeState(
        run_id=RunId("run_resume"),
        engagement_id=EngagementId("eng_recovered"),
        termination_reason=TerminationReason.APPROVAL_PENDING,
        pending_approval_id=ApprovalId("appr_1"),
        pending_action_id=ActionId("action_1"),
    )
    _FakeRunner.recovered = recovered
    _FakeRunner.final = _finished()
    _FakeRunner.resumed_with = None
    _FakeRunner.closed = False

    hint = AsyncMock(return_value=_identity("eng_recovered"))
    build = AsyncMock(return_value=(object(), deps))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", hint)
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    settle = AsyncMock(return_value=_finished())
    monkeypatch.setattr(resume_command, "settle_engagement", settle)

    exit_code = await resume_command.run(_args(engagement_path))

    assert exit_code == EXIT_OK
    build.assert_awaited_once()
    build_call = build.await_args
    assert build_call is not None
    assert build_call.kwargs["engagement_id"] == EngagementId("eng_recovered")
    called_config = build_call.args[0]
    assert called_config.engagement.name == "Resume Test"
    assert _FakeRunner.resumed_with is recovered
    assert settle.await_count == 1
    assert _FakeRunner.closed is True


async def test_resume_fails_closed_without_a_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=None))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    exit_code = await resume_command.run(_args(engagement_path))

    assert exit_code == EXIT_FAILURE
    build.assert_not_awaited()


async def test_resume_fails_closed_without_a_safe_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    deps = build_test_application()
    hint = _identity("eng_recovered")
    _FakeRunner.recovered = None  # recover_runtime refuses an unsafe snapshot

    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(
        resume_command, "build_resume_application", AsyncMock(return_value=(object(), deps))
    )
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(resume_command, "settle_engagement", AsyncMock())

    exit_code = await resume_command.run(_args(engagement_path))

    assert exit_code == EXIT_FAILURE


async def test_resume_reports_bad_mcp_configuration_as_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path = _engagement_yaml(tmp_path)
    hint = _identity("eng_recovered")
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(
        resume_command,
        "build_resume_application",
        AsyncMock(side_effect=MCPConfigurationError("missing REDTEAM_MCP_X")),
    )

    exit_code = await resume_command.run(_args(engagement_path))

    assert exit_code == EXIT_CONFIG_ERROR


async def test_resume_paused_run_applies_operator_intent_before_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _engagement_yaml(tmp_path)
    mission_id = MissionId("mission_paused")
    store = InMemoryTaskGraphStore()
    now = datetime.now(timezone.utc)
    await store.create_mission(
        MissionPlan(
            mission_id=mission_id,
            engagement_id=EngagementId("eng_recovered"),
            objective="Continue safely",
            status=MissionStatus.PAUSED,
            provenance=Provenance(source="operator"),
            created_at=now,
            updated_at=now,
        )
    )
    deps = replace(
        build_test_application(),
        task_graph_store=store,
        operator_intent_store=InMemoryOperatorIntentStore(),
    )
    await coordinator_for(store, deps.path_store).bind(
        "run_resume", str(mission_id), "eng_recovered"
    )
    paused = RuntimeState(
        run_id=RunId("run_resume"),
        engagement_id=EngagementId("eng_recovered"),
        mission_id=mission_id,
        phase=AgentPhase.FINISHED,
        termination_reason=TerminationReason.OPERATOR_PAUSED,
    )
    _FakeRunner.recovered = paused
    _FakeRunner.final = paused.model_copy(
        update={"termination_reason": TerminationReason.NO_ACTIONABLE_PLAN}
    )
    hint = _identity("eng_recovered", mission_id=str(mission_id))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(
        resume_command, "build_resume_application", AsyncMock(return_value=(object(), deps))
    )
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(
        resume_command, "settle_engagement", AsyncMock(return_value=_FakeRunner.final)
    )

    assert await resume_command.run(_args(path)) == EXIT_OK
    mission = await store.get_mission(mission_id)
    assert mission is not None
    assert mission.status is MissionStatus.ACTIVE
    assert deps.operator_intent_store is not None
    intents = await deps.operator_intent_store.list_for_mission(mission_id)
    assert len(intents) == 1 and intents[0].intent.intent_type.value == "resume"


async def test_pending_approval_without_resolution_is_not_reported_as_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _engagement_yaml(tmp_path)
    pending = RuntimeState(
        run_id=RunId("run_resume"),
        engagement_id=EngagementId("eng_recovered"),
        termination_reason=TerminationReason.APPROVAL_PENDING,
    )
    _FakeRunner.recovered = pending
    _FakeRunner.final = pending
    hint = _identity("eng_recovered")
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(
        resume_command,
        "build_resume_application",
        AsyncMock(return_value=(object(), build_test_application())),
    )
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    settle = AsyncMock()
    monkeypatch.setattr(resume_command, "settle_engagement", settle)
    assert await resume_command.run(_args(path)) == EXIT_FAILURE
    settle.assert_not_awaited()


async def test_resume_restores_control_counters_and_elapsed_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _engagement_yaml(tmp_path)
    start = datetime.now(timezone.utc)
    recovered = RuntimeState(
        run_id=RunId("run_resume"),
        engagement_id=EngagementId("eng_recovered"),
        actions_executed=3,
        retry_tool_calls=2,
        started_at=start,
        termination_reason=TerminationReason.APPROVAL_PENDING,
    )
    _FakeRunner.recovered = recovered
    _FakeRunner.final = recovered
    control = type("RecordingControl", (), {"rehydrate": AsyncMock()})()
    deps = replace(build_test_application(), control_plane=control)
    hint = _identity("eng_recovered")
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(
        resume_command, "build_resume_application", AsyncMock(return_value=(object(), deps))
    )
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    assert await resume_command.run(_args(path)) == EXIT_FAILURE
    control.rehydrate.assert_awaited_once()
    call = control.rehydrate.await_args
    assert call.args == (EngagementId("eng_recovered"),)
    assert call.kwargs["actions_consumed"] == 3
    assert call.kwargs["tool_calls_consumed"] == 5
    assert 0 <= call.kwargs["elapsed_duration_seconds"] < 5


def _hint_with_binding(tmp_path: Path, binding) -> tuple:
    return _engagement_yaml(tmp_path), _identity("eng_recovered", binding=binding)


async def test_resume_rejects_unregistered_profile_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path, hint = _hint_with_binding(
        tmp_path,
        RuntimeComponentBinding(
            profile_id="custom",
            version="9",
            component_ids=dict(BUILTIN_COMPONENT_IDS),
        ),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    assert await resume_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()


async def test_resume_rejects_changed_component_ids_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engagement_path, hint = _hint_with_binding(
        tmp_path,
        RuntimeComponentBinding(
            profile_id="builtin",
            version="1",
            component_ids=dict(BUILTIN_COMPONENT_IDS, agents="custom-agents-v9"),
        ),
    )
    build = AsyncMock()
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)

    assert await resume_command.run(_args(engagement_path)) == EXIT_FAILURE
    build.assert_not_awaited()


async def test_resume_forwards_stored_builtin_selection_to_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = RuntimeComponentBinding(
        profile_id="builtin",
        version="1",
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )
    engagement_path, hint = _hint_with_binding(tmp_path, binding)
    deps = build_test_application()
    recovered = RuntimeState(
        run_id=RunId("run_resume"),
        engagement_id=EngagementId("eng_recovered"),
        termination_reason=TerminationReason.APPROVAL_PENDING,
        component_binding=binding,
    )
    _FakeRunner.recovered = recovered
    _FakeRunner.final = _finished()
    _FakeRunner.resumed_with = None
    _FakeRunner.closed = False
    build = AsyncMock(return_value=(object(), deps))
    monkeypatch.setattr(resume_command, "_read_checkpoint_identity", AsyncMock(return_value=hint))
    monkeypatch.setattr(resume_command, "build_resume_application", build)
    monkeypatch.setattr(resume_command, "AgentLoopRunner", _FakeRunner)
    monkeypatch.setattr(resume_command, "settle_engagement", AsyncMock(return_value=_finished()))

    assert await resume_command.run(_args(engagement_path)) == EXIT_OK
    build.assert_awaited_once()
    assert build.await_args is not None
    assert build.await_args.kwargs["component_profile"] == ComponentProfile(
        profile_id="builtin", version="1"
    )


async def test_identity_reads_msgpack_without_running_constructors_and_strict_holds(
    tmp_path: Path,
) -> None:
    _CONSTRUCTED.clear()
    selected = StateCodecRegistry()
    selected.register(_IdentityProbe.__module__, "_IdentityProbe", version="7")
    binding = RuntimeComponentBinding(
        profile_id="builtin",
        version="1",
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )
    run_id = "run-identity-msgpack"
    path = tmp_path / "identity.sqlite3"
    marker = _IdentityProbe(label="kept")
    _CONSTRUCTED.clear()
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "ts": "2026-01-01T00:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000002",
        "channel_values": {
            "runtime": {
                "engagement_id": "eng-identity",
                "mission_id": "mission-1",
                "component_binding": binding.model_dump(mode="json"),
                "extra": marker,
            }
        },
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    async with open_checkpointer(backend="sqlite", path=str(path), registry=selected) as saver:
        assert saver is not None
        await saver.aput(config, checkpoint, {}, {})
    _CONSTRUCTED.clear()
    found = await read_checkpoint_identity(
        enabled=True, backend="sqlite", path=str(path), run_id=run_id
    )
    assert found is not None
    assert found.engagement_id == EngagementId("eng-identity")
    assert found.mission_id == "mission-1"
    assert found.component_binding is not None
    assert found.component_binding.profile_id == "builtin"
    assert _CONSTRUCTED == []
    async with open_checkpointer(
        backend="sqlite", path=str(path), registry=selected
    ) as strict_saver:
        assert strict_saver is not None
        restored = await strict_saver.aget(config)
    assert restored is not None
    extra = restored["channel_values"]["runtime"]["extra"]
    assert isinstance(extra, _IdentityProbe)
    assert extra.label == "kept"


async def test_identity_handles_legacy_json_envelope_kwargs_without_construction() -> None:
    _CONSTRUCTED.clear()
    selected = StateCodecRegistry()
    selected.register(_IdentityProbe.__module__, "_IdentityProbe", version="7")
    binding = RuntimeComponentBinding(
        profile_id="builtin",
        version="1",
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )
    envelope_id = _IdentityProbe.__module__.split(".") + ["_IdentityProbe"]
    payload = {
        "runtime": {
            "engagement_id": "eng-legacy",
            "mission_id": "mission-legacy",
            "component_binding": binding.model_dump(mode="json"),
            "evil": {
                "lc": 2,
                "type": "constructor",
                "id": envelope_id,
                "kwargs": {"label": "kept"},
            },
        }
    }
    raw = ("json", json.dumps(payload).encode("utf-8"))
    async with open_identity_reader(backend="memory") as reader:
        assert reader is not None
        projected = reader.serde.loads_typed(raw)
    assert projected["runtime"]["engagement_id"] == "eng-legacy"
    assert projected["runtime"]["evil"] == {"label": "kept"}
    assert _CONSTRUCTED == []
    strict = build_checkpointer(enabled=True, registry=selected)
    assert strict is not None
    revived = strict.serde.loads_typed(raw)
    evil = revived["runtime"]["evil"]
    assert isinstance(evil, _IdentityProbe)
    assert evil.label == "kept"
