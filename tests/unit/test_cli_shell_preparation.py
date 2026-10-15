"""Execution preparation reaches CLI composition and durable resume identity."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli.commands import run, tools, validate
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.settings import (
    MCPConfigurationError,
    automatic_shell_setup_from_environment,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_runtime import prepare_shell_runtime
from redteam.core.config import AppConfig, EngagementConfig
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.component_binding import builtin_component_binding
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEventBus, FakeEvidenceStore
from tests.unit.test_cli_bootstrap_wiring import _engagement_yaml as _cli_engagement_yaml
from tests.unit.test_cli_validate import _args

IMAGE = "sha256:" + "ab" * 32
FINGERPRINT = "cd" * 32


def _fake_preparation(monkeypatch, tmp_path):
    manifest = tmp_path / "program-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "image_digest": IMAGE,
                "programs": [
                    {
                        "version": "1",
                        "origin": None,
                        "executables": [
                            {"command": "nmap", "path": "/usr/bin/nmap"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ShellWorkroomPolicy(image=IMAGE, docker_context="desktop-linux")
    prepare = AsyncMock(
        return_value=SimpleNamespace(
            policy=policy,
            manifest_path=str(manifest),
            fingerprint=FINGERPRINT,
            endpoint=SimpleNamespace(
                binary="docker", context="desktop-linux", host="unix:///docker.sock",
            ),
            network_policy=None,
            network_policy_hash=None,
            status="local_only",
            readiness_checked_at=None,
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "redteam.capability.shell_preparation",
        SimpleNamespace(
            prepare_shell_policy=prepare,
        ),
    )
    monkeypatch.delenv("REDTEAM_SHELL_POLICY_PATH", raising=False)
    monkeypatch.delenv("REDTEAM_GATED_NETWORK_POLICY_PATH", raising=False)
    monkeypatch.setenv("REDTEAM_SHELL_AUTO_SETUP", "true")
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", '{"mode":"bounded","max_requests":10}')
    return prepare


def test_auto_setup_defaults_enabled_and_invalid_values_are_bounded():
    assert automatic_shell_setup_from_environment(source={}) is True
    assert (
        automatic_shell_setup_from_environment(
            source={
                "REDTEAM_SHELL_AUTO_SETUP": "false",
            }
        )
        is False
    )
    with pytest.raises(MCPConfigurationError) as caught:
        automatic_shell_setup_from_environment(
            source={
                "REDTEAM_SHELL_AUTO_SETUP": "secret-invalid-value",
            }
        )
    assert "secret-invalid-value" not in str(caught.value)


async def test_prepared_manifest_reaches_no_argument_program_discovery(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    runtime = await prepare_shell_runtime(None)
    assert runtime is not None
    assert runtime.environment_fingerprint == FINGERPRINT
    assert runtime.program_source is runtime.adapter.program_source
    monkeypatch.setattr(runtime.program_source, "_resolve_image_id", AsyncMock(return_value=IMAGE))
    rows = await runtime.program_source.list_image_programs()
    assert [(row.name, row.executable) for row in rows] == [("nmap", "/usr/bin/nmap")]
    prepare.assert_awaited_once_with(
        None, progress=None, expected_fingerprint=None,
    )


async def test_manual_mode_never_prepares_or_builds(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    assert await prepare_shell_runtime(None, auto_setup=False) is None
    prepare.assert_not_awaited()


async def test_recorded_environment_cannot_be_disabled(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    with pytest.raises(MCPConfigurationError, match="requires its prepared"):
        await prepare_shell_runtime(None, auto_setup=False, expected_fingerprint=FINGERPRINT)
    prepare.assert_not_awaited()


async def test_run_stamps_environment_and_injects_same_adapter(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    deps = replace(
        RuntimeDeps(
            control_plane=FakeControlPlane(),
            tool_gateway=DummyToolGateway(),
            event_bus=FakeEventBus(),
            graph_store=InMemoryAttackGraphStore(),
            evidence_store=FakeEvidenceStore(),
            llm_client=cast(Any, None),
            central_state=CentralStateStore(tmp_path / "state.db",
                                            run_id="run-test", engagement_id="eng-test"),
        ),
        component_binding=builtin_component_binding(),
    )
    build = AsyncMock(return_value=("engagement", deps))
    monkeypatch.setattr(run, "build_application", build)
    config = AppConfig(
        engagement=EngagementConfig(name="Shell preparation", execution_budget={"max_actions": 1})
    )
    engagement, result = await run.build(config, max_steps=1)
    assert engagement == "engagement" and result is not deps
    assert result.component_binding is not None
    assert result.component_binding.execution_environment_fingerprint == FINGERPRINT
    kwargs = build.await_args.kwargs
    assert kwargs["shell_programs"] is kwargs["builtin_adapters"][0].program_source
    assert kwargs["shell_programs"].policy.docker_context == "desktop-linux"
    assert "gated_shell" not in kwargs
    prepare.assert_awaited_once()


async def test_resume_restores_recorded_environment(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    deps = replace(
        RuntimeDeps(
            control_plane=FakeControlPlane(),
            tool_gateway=DummyToolGateway(),
            event_bus=FakeEventBus(),
            graph_store=InMemoryAttackGraphStore(),
            evidence_store=FakeEvidenceStore(),
            llm_client=cast(Any, None),
            central_state=CentralStateStore(tmp_path / "state.db",
                                            run_id="run-test", engagement_id="eng-test"),
        ),
        component_binding=builtin_component_binding(),
    )
    build = AsyncMock(return_value=("engagement", deps))
    monkeypatch.setattr(run, "build_application", build)
    config = AppConfig(
        engagement=EngagementConfig(name="Shell preparation", execution_budget={"max_actions": 1})
    )
    _, result = await run.build(
        config,
        max_steps=None,
        original_settings=OrchestrationSettings(),
        original_stores={"evidence": "e", "audit": "a", "approvals": "p"},
        original_component_binding=builtin_component_binding().model_copy(
            update={
                "execution_environment_fingerprint": FINGERPRINT,
            }
        ),
    )
    assert prepare.await_args.kwargs["expected_fingerprint"] == FINGERPRINT
    assert result.component_binding is not None
    assert result.component_binding.execution_environment_fingerprint == FINGERPRINT


async def test_legacy_resume_does_not_enable_new_default(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    build = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(run, "build_application", build)
    config = AppConfig(
        engagement=EngagementConfig(name="Shell preparation", execution_budget={"max_actions": 1})
    )
    await run.build(
        config,
        max_steps=None,
        original_settings=OrchestrationSettings(),
        original_stores={"evidence": "e", "audit": "a", "approvals": "p"},
    )
    prepare.assert_not_awaited()
    assert "builtin_adapters" not in build.await_args.kwargs


async def test_preparation_failure_prevents_application_start(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    prepare.side_effect = MCPConfigurationError("Install Docker Desktop")
    build = AsyncMock()
    monkeypatch.setattr(run, "build_application", build)
    config = AppConfig(
        engagement=EngagementConfig(name="Shell preparation", execution_budget={"max_actions": 1})
    )
    with pytest.raises(MCPConfigurationError, match="Install Docker Desktop"):
        await run.build(config, max_steps=1)
    build.assert_not_awaited()


async def test_offline_validate_reports_auto_setup_without_provisioning(
    monkeypatch,
    tmp_path,
    capsys,
):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    path = _cli_engagement_yaml(tmp_path)
    assert await validate.run(_args(path, json_mode=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("automatic preparation" in row["name"] for row in payload["checks"])
    assert payload["live_readiness_checked"] is False
    prepare.assert_not_awaited()


async def test_tools_inspection_does_not_build_default(monkeypatch, tmp_path):
    prepare = _fake_preparation(monkeypatch, tmp_path)
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    await tools._build_gateway()
    prepare.assert_not_awaited()


def test_optional_binding_omission_preserves_legacy_serialization():
    binding = builtin_component_binding()
    assert "execution_environment_fingerprint" not in binding.model_dump(mode="json")
    updated = binding.model_copy(update={"execution_environment_fingerprint": FINGERPRINT})
    assert updated.model_dump(mode="json")["execution_environment_fingerprint"] == FINGERPRINT


def test_docker_context_pinned_in_execution_command():
    policy = ShellWorkroomPolicy(image=IMAGE, docker_context="desktop-linux")
    assert policy.docker_command("info") == ("docker", "--context", "desktop-linux", "info")
    assert ShellWorkroomPolicy(image=IMAGE).docker_command("info") == ("docker", "info")


async def test_gate_daemon_uses_prepared_context(monkeypatch):
    import asyncio

    from redteam.capability.gated_window import GatedWindowExtension
    from redteam.capability.shell_workroom import ShellWorkroomManager

    manager = ShellWorkroomManager(ShellWorkroomPolicy(image=IMAGE, docker_context="prepared"))
    extension = GatedWindowExtension(
        manager=manager,
        ledger=cast(Any, None),
        run_docker=cast(Any, None),
    )
    captured = []

    async def capture(*argv, **kwargs):
        captured.extend(argv)
        raise RuntimeError("captured launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    window = SimpleNamespace(
        daemon_session_id="test-session",
        container_tag="test-container",
        grant=SimpleNamespace(window_id="test-window", epoch=1),
    )
    with pytest.raises(RuntimeError, match="captured launch"):
        await extension._rpc_loop(cast(Any, window))
    assert captured[:5] == ["docker", "--context", "prepared", "exec", "-i"]
