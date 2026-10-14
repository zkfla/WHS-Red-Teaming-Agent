"""Central CLI refuses changed Go routing before shell/MCP/model assembly."""

from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands import resume, run
from redteam.core.config import AppConfig, EngagementConfig, LLMConfig, OrchestrationConfig
from redteam.llm.fake import FakeLLMClient
from redteam.llm.opencode_go_config import llm_connection_fingerprint_for_config
from tests.integration.test_central_completion import OriginalProtocolDouble, selection
from tests.integration.test_central_development import gateway
from tests.integration.test_central_recovery import start


def go_config(model="openai/model-a", key="TEST_GO_KEY", effort=None):
    return LLMConfig(provider="opencode-go", default_model=model, api_key_env_var=key,
                     reasoning_effort=effort)


class PinnedFakeClient(FakeLLMClient):
    def __init__(self, config):
        super().__init__()
        self.connection_fingerprint = llm_connection_fingerprint_for_config(config)


def cli_config(session, llm):
    return AppConfig(
        engagement=EngagementConfig.model_validate(
            session.deps.engagement.model_dump(exclude={"engagement_id", "created_at"})
        ),
        llm=llm,
        orchestration=OrchestrationConfig(
            central_state_path=str(session.deps.central_state.path),
            specialist_profile_path=session.deps.settings.specialist_profile_path,
        ),
    )


@pytest.mark.parametrize(
    "original,changed",
    [
        (go_config(), go_config("openai/model-b")),
        (go_config(), go_config("anthropic/model-a")),
        (go_config(), go_config(key="OTHER_KEY")),
        (go_config(), LLMConfig()),
        (go_config(), go_config(effort="high")),
        (go_config(effort="high"), go_config(effort="low")),
        (go_config(effort="high"), go_config()),
        (LLMConfig(reasoning_effort="high"), LLMConfig(reasoning_effort="low")),
        (LLMConfig(reasoning_effort="high"), LLMConfig()),
    ],
)
@pytest.mark.parametrize("collect", [False, True])
async def test_changed_connection_refused_before_any_rebuild(
    tmp_path, monkeypatch, original, changed, collect
):
    async with selection(
        tmp_path, llm=PinnedFakeClient(original), tools=OriginalProtocolDouble(gateway(risk="high"))
    ) as (session, llm):
        await start(session, llm)
        _, stored, _ = await inspect_central_run(
            session.deps.central_state.path, session.runtime.run_id
        )
        assert stored["component_binding"]["llm_connection_fingerprint"] == (
            llm.connection_fingerprint
        )
        prepare, build = AsyncMock(), AsyncMock()
        monkeypatch.setattr(run, "prepare_shell_runtime", prepare)
        monkeypatch.setattr(run, "build_application", build)
        args = SimpleNamespace(
            run_id=str(session.runtime.run_id),
            instance=None,
            max_steps=None,
            workers=None,
            json=True,
        )
        dispatch = resume._run_collect_central if collect else resume._run_central
        async with AsyncExitStack() as stack:
            assert await dispatch(args, cli_config(session, changed), mcp_stack=stack) == 1
        prepare.assert_not_awaited()
        build.assert_not_awaited()


@pytest.mark.parametrize("effort", [None, "high"])
async def test_same_go_connection_and_key_rotation_can_rebuild(tmp_path, monkeypatch, effort):
    config = go_config(effort=effort)
    async with selection(tmp_path, llm=PinnedFakeClient(config)) as (session, llm):
        _, stored, _ = await inspect_central_run(
            session.deps.central_state.path, session.runtime.run_id
        )
        monkeypatch.setenv(config.api_key_env_var, "rotated-fixture-key")
        prepare = AsyncMock(return_value=None)
        build = AsyncMock(return_value=(session.deps.engagement, session.deps))
        monkeypatch.setattr(run, "prepare_shell_runtime", prepare)
        monkeypatch.setattr(run, "build_application", build)
        await run.build(
            cli_config(session, config),
            max_steps=None,
            central_state=session.deps.central_state,
            original_settings=session.deps.settings,
            original_stores=stored["storage_refs"],
            original_component_binding=stored["component_binding"],
        )
        build.assert_awaited_once()
        assert prepare.await_args.kwargs["auto_setup"] is False
