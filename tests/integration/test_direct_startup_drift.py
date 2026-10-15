"""Direct web startup through real run_session/build/validate; drift checks only.

Only the Docker shell runtime, the composition root (build_application), and the
execute step are replaced. Web deployment generation, the engagement install
compare-and-swap, startup selection, the drift check in run_session, and the
read-only validate path are real.
"""

import argparse
import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from redteam.app import web_policy as web_policy_module
from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_OK, load_app_config_for_run
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.commands import validate as validate_command
from redteam.app.cli.conversation import ConversationAssistant
from redteam.app.cli.interactive_commands import InteractiveSession
from redteam.app.cli.session_runtime import run_session
from redteam.app.cli.terminal_frontend import ConsoleFrontend
from redteam.app.web.engagement import EngagementStore
from redteam.app.web_policy import select_engagement_startup
from redteam.core.config import load_engagement_config
from redteam.core.environment import environment_context
from redteam.llm.fake import FakeLLMClient

NAME = "local lab"
DRAFT = {
    "name": NAME,
    "allowed_hosts": ["https://127.0.0.1:8443"],
    "allowed_capabilities": ["web"],
    "max_actions": 5,
    "web_policy_mode": "site",
    # Closed Kali selection keeps the target network away from Docker.
    "kali_network_mode": "closed",
}


def save(path: Path, **changes) -> EngagementStore:
    store = EngagementStore(path)
    store.save({**DRAFT, **changes}, confirm_scope=True, confirm_expansion=True)
    return store


class _NoInputFrontend(ConsoleFrontend):
    """Single-run frontend: the objective is supplied by the caller and no line is read."""

    async def read_line(self) -> str:
        raise EOFError


def _build_application_with_hook(build_hook):
    async def fake_build_application(app_config, **kwargs):
        if build_hook is not None:
            build_hook()
        return object(), SimpleNamespace()

    return fake_build_application


def _patch_startup(monkeypatch, *, build_hook=None):
    """Fake only the Docker runtime, the composition root, and the execute step."""
    executed = []

    async def no_docker_shell_runtime(policy_path, **kwargs):
        return None

    async def fake_execute(engagement, deps, **kwargs):
        executed.append(engagement)
        raise RuntimeError("sentinel after startup")

    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.delenv("REDTEAM_SHELL_POLICY_PATH", raising=False)
    monkeypatch.setattr(run_command, "prepare_shell_runtime", no_docker_shell_runtime)
    monkeypatch.setattr(run_command, "build_application", _build_application_with_hook(build_hook))
    monkeypatch.setattr(run_command, "execute", fake_execute)
    return executed


async def _run_once(store: EngagementStore):
    config = load_app_config_for_run(str(store.path))
    session = InteractiveSession(config.engagement, config.llm, engagement_path=store.path)
    return await run_session(
        Console(file=io.StringIO()),
        config,
        conversation=ConversationAssistant(FakeLLMClient()),
        session=session,
        objective="startup regression",
        frontend=_NoInputFrontend(Console(file=io.StringIO())),
        single_run=True,
    )


async def _restart_once(store: EngagementStore):
    """Same order as the production run command: hydrate the recorded startup, then load."""
    startup = await select_engagement_startup(store.path)
    with environment_context(startup.environment):
        config = load_app_config_for_run(str(store.path))
    session = InteractiveSession(config.engagement, config.llm, engagement_path=store.path)
    return await run_session(
        Console(file=io.StringIO()),
        config,
        conversation=ConversationAssistant(FakeLLMClient()),
        session=session,
        objective="startup regression",
        frontend=_NoInputFrontend(Console(file=io.StringIO())),
        single_run=True,
    )


async def _validate(store: EngagementStore, monkeypatch) -> int:
    # Validate needs an explicit runtime budget; the default specialist profile requires one.
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", '{"mode":"bounded","max_requests":10}')
    return await validate_command.run(
        argparse.Namespace(engagement_path=str(store.path), json=True),
    )


def _operator_edit(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    edited = text.replace(NAME, "operator edit")
    assert edited != text
    path.write_text(edited, encoding="utf-8")
    assert load_engagement_config(path).name == "operator edit"


def _snapshot(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


async def test_startup_writes_of_the_deployment_are_not_operator_drift(tmp_path, monkeypatch):
    store = save(tmp_path / "unprepared.yaml")
    before = store.path.read_bytes()
    executed = _patch_startup(monkeypatch)

    first = await _run_once(store)

    assert store.path.read_bytes() != before
    assert "mcp_deployment" in store.load().metadata
    assert len(executed) == 1
    assert "sentinel after startup" in (first.error or "")
    assert "설정이 변경되어" not in (first.error or "")


async def test_restart_of_recorded_deployment_dispatches_and_keeps_operator_guard(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "restart.yaml")
    executed = _patch_startup(monkeypatch)

    first = await _run_once(store)
    assert "sentinel after startup" in (first.error or "")
    assert len(executed) == 1

    restarted = await _restart_once(store)

    assert "MCP deployment validation failed" not in (restarted.error or "")
    assert "sentinel after startup" in (restarted.error or "")
    assert len(executed) == 2

    # The restart guard still refuses a real operator edit made during bootstrap.
    monkeypatch.setattr(
        run_command, "build_application",
        _build_application_with_hook(lambda: _operator_edit(store.path)),
    )
    edited = await _restart_once(store)

    assert edited.exit_code == 2
    assert "설정이 변경되어 시작하지 않았습니다" in (edited.error or "")
    assert len(executed) == 2
    assert store.load().name == "operator edit"


async def test_operator_edit_during_build_is_drift_and_never_dispatches(tmp_path, monkeypatch):
    store = save(tmp_path / "drift.yaml")
    executed = _patch_startup(
        monkeypatch, build_hook=lambda: _operator_edit(store.path),
    )

    result = await _run_once(store)

    assert executed == []
    assert result.exit_code == 2
    assert "설정이 변경되어 시작하지 않았습니다" in (result.error or "")
    assert store.load().name == "operator edit"


async def test_operator_edit_before_install_refuses_install_without_overwrite(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "install-window.yaml")
    original_provision = web_policy_module.provision

    def provision_then_operator_edit(spec, engagement, output):
        generated = original_provision(spec, engagement, output)
        _operator_edit(store.path)
        return generated

    monkeypatch.setattr(web_policy_module, "provision", provision_then_operator_edit)
    executed = _patch_startup(monkeypatch)

    result = await _run_once(store)

    assert executed == []
    assert result.exit_code != 0
    assert "EngagementError" in (result.error or "")
    loaded = store.load()
    assert loaded.name == "operator edit"
    assert "mcp_deployment" not in loaded.metadata


async def test_validate_unprepared_web_declaration_checks_offline_without_artifacts(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "unprepared-validate.yaml")
    before = _snapshot(tmp_path)

    assert await _validate(store, monkeypatch) == EXIT_OK
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "unprepared-validate.mcp").exists()


async def test_validate_recorded_restart_is_valid_and_read_only(tmp_path, monkeypatch):
    store = save(tmp_path / "validated.yaml")
    _patch_startup(monkeypatch)
    await _run_once(store)  # the first startup records the deployment
    before = _snapshot(tmp_path)

    assert await _validate(store, monkeypatch) == EXIT_OK
    assert _snapshot(tmp_path) == before


async def test_validate_recorded_restart_rejects_changed_policy_digest_read_only(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "digest.yaml")
    _patch_startup(monkeypatch)
    await _run_once(store)
    policy = Path(store.load().metadata["mcp_deployment"]["policy_path"])
    policy.write_bytes(policy.read_bytes() + b"\n")
    before = _snapshot(tmp_path)

    assert await _validate(store, monkeypatch) == EXIT_CONFIG_ERROR
    assert _snapshot(tmp_path) == before


async def test_validate_masks_unparseable_recorded_artifact_and_stays_read_only(
    tmp_path, monkeypatch, capsys,
):
    store = save(tmp_path / "parse.yaml")
    _patch_startup(monkeypatch)
    await _run_once(store)
    source = Path(store.load().metadata["mcp_deployment"]["source_path"])
    source.write_text('{"SECRET-MARKER": ', encoding="utf-8")
    before = _snapshot(tmp_path)
    capsys.readouterr()

    assert await _validate(store, monkeypatch) == EXIT_CONFIG_ERROR
    output = capsys.readouterr().out
    assert "MCP deployment validation failed" in output
    assert "SECRET-MARKER" not in output
    assert _snapshot(tmp_path) == before
