"""CLI configuration reaches the runtime and each new Mission reads current ROE."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from redteam.app.cli._shared import load_app_config_for_run
from redteam.app.cli.commands import interactive
from redteam.app.cli.conversation import ConversationAssistant
from redteam.app.cli.interactive_commands import InteractiveSession
from redteam.core.config import (
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    LoggingConfig,
    OrchestrationConfig,
)
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, RunId
from redteam.intake.serializer import dump_engagement_yaml
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from tests.integration.test_cli_session import _ConversationLLM, _FakeConsole


def _engagement(host="old.example", budget=10):
    return EngagementConfig.model_validate(
        {
            "name": "config test",
            "scope": {"allowed_hosts": [host]},
            "execution_budget": {"max_actions": budget},
        }
    )


async def test_runtime_environment_reaches_both_cli_entrypoints(tmp_path, monkeypatch):
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.setenv("REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION", "true")
    monkeypatch.setenv("REDTEAM_ORCHESTRATION_MAX_STEPS", "17")
    path = tmp_path / "engagement.yaml"
    path.write_text(dump_engagement_yaml(_engagement()), encoding="utf-8")
    config = load_app_config_for_run(str(path))
    _, interactive_config, _, _ = await interactive._ensure_runtime_config(_FakeConsole([]))
    assert config.orchestration == interactive_config
    settings = OrchestrationSettings.for_durable_run(config.orchestration)
    assert settings.llm_candidate_generation is True
    assert settings.max_steps == 17
    assert settings.tool_timeout_seconds == 20
    assert OrchestrationSettings.for_durable_run(config.orchestration, max_steps=9).max_steps == 9


def test_new_tool_timeout_does_not_change_a_saved_run():
    current = OrchestrationSettings.for_durable_run(OrchestrationConfig())
    assert current.tool_timeout_seconds == 20

    earlier = current.with_overrides(tool_timeout_seconds=10)
    restored = OrchestrationSettings.model_validate(earlier.durable_payload())
    assert restored.tool_timeout_seconds == 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("LLM_CANDIDATE_GENERATION", "perhaps"),
        ("MAX_STEPS", "0"),
    ],
)
def test_invalid_runtime_environment_is_rejected(monkeypatch, field, value):
    monkeypatch.setenv(f"REDTEAM_ORCHESTRATION_{field}", value)
    with pytest.raises(ValidationError):
        OrchestrationConfig.from_environment()


def test_removed_retry_environment_is_rejected(monkeypatch):
    # F21: the retry knob was removed from the public contract; new input is
    # rejected explicitly instead of silently ignored.
    monkeypatch.setenv("REDTEAM_ORCHESTRATION_MAX_RETRIES", "2")
    with pytest.raises(ValueError, match="REDTEAM_ORCHESTRATION_MAX_RETRIES"):
        OrchestrationConfig.from_environment()


async def _session(console, session):
    return await interactive._session_loop(
        console,
        session,
        llm_config=LLMConfig(),
        orchestration_config=OrchestrationConfig(),
        capability_config=CapabilityConfig(),
        logging_config=LoggingConfig(),
        verbose=False,
        conversation=ConversationAssistant(_ConversationLLM()),
    )


async def test_next_mission_uses_current_scope_and_budget(tmp_path, monkeypatch):
    path = tmp_path / "engagement.yaml"
    new = _engagement("new.example", 2)
    path.write_text(dump_engagement_yaml(new), encoding="utf-8")
    session = InteractiveSession(
        engagement_config=_engagement(),
        llm_config=LLMConfig(),
        engagement_path=path,
    )
    execute = AsyncMock(
        return_value=RuntimeState(
            run_id=RunId("run_config"),
            engagement_id=EngagementId("eng_config"),
            phase=AgentPhase.FINISHED,
        )
    )
    monkeypatch.setattr(interactive.run_command, "execute", execute)
    monkeypatch.setattr(
        interactive.run_command,
        "build",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
    )
    await _session(_FakeConsole(["assess the scope", "/exit"]), session)
    assert execute.await_count == 1
    assert execute.call_args.kwargs["config"].engagement == new
    assert session.engagement_config == new


@pytest.mark.parametrize("case", ["missing", "malformed", "empty_scope"])
async def test_invalid_file_never_falls_back_to_cached_scope(tmp_path, monkeypatch, case):
    path = tmp_path / "engagement.yaml"
    if case == "malformed":
        path.write_text("name: [", encoding="utf-8")
    elif case == "empty_scope":
        config = _engagement()
        config.scope.allowed_hosts = []
        path.write_text(dump_engagement_yaml(config), encoding="utf-8")
    session = InteractiveSession(
        engagement_config=_engagement(),
        llm_config=LLMConfig(),
        engagement_path=path,
    )
    execute = AsyncMock(return_value=(0, None))
    monkeypatch.setattr(interactive.run_command, "execute", execute)
    assert await _session(_FakeConsole(["assess the scope", "/exit"]), session) == 0
    execute.assert_not_awaited()
