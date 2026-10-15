"""Confirmed web selections and scoped MCP assembly; temporary files only."""

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.app import web_policy as web_policy_module
from redteam.app.cli._shared import load_app_config_for_run
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.interactive_commands import InteractiveSession
from redteam.app.cli.session_runtime import run_session
from redteam.app.cli.terminal_frontend import ConsoleFrontend
from redteam.app.mcp_deployment import DeploymentBinding, synchronize_deployment
from redteam.app.web.engagement import EngagementError, EngagementStore
from redteam.app.web.host import RealLauncher
from redteam.app.web_policy import (
    GATED_PREPARATION_KEY,
    WebPolicySelection,
    _load_prepared_policy,
    prepare_web_deployment,
    select_engagement_startup,
)
from redteam.capability.deployment import DeploymentSpec
from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    GatedNetworkPolicyRequest,
    build_gated_network_policy,
    policy_content_hash,
)
from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.servers.network import ToolInputError, validate_path
from redteam.core.config import load_engagement_config
from redteam.core.environment import env_snapshot, environment_context

DRAFT = {"name": "local lab", "allowed_hosts": ["https://127.0.0.1:8443"],
         "allowed_capabilities": ["web"], "max_actions": 5, "web_policy_mode": "site"}
PREPARE = {
    "kali_network_mode": "prepare",
    "kali_endpoints": [{
        "origin": "https://127.0.0.1:8443", "port": 8443, "transport": "tcp",
    }],
    "kali_limits_preset": "probe",
    "kali_sidecar_mode": "pinned",
    "kali_sidecar_image": "example.invalid/gate@sha256:" + "a" * 64,
}


def save(path, **changes):
    store = EngagementStore(path)
    store.save({**DRAFT, **changes}, confirm_scope=True, confirm_expansion=True)
    return store


@pytest.mark.parametrize("mode,paths", [("site", []), ("paths", ["/", "/login"])])
async def test_confirmed_selection_generates_valid_private_policy_and_reuses_it(
    tmp_path, mode, paths,
):
    store = save(tmp_path / "lab.yaml", web_policy_mode=mode, web_paths=paths)
    original = store.load()
    with environment_context({}):
        environment = await prepare_web_deployment(store.path)
    loaded = store.load()
    assert loaded.scope == original.scope
    assert loaded.rules_of_engagement == original.rules_of_engagement
    assert loaded.execution_budget == original.execution_budget
    assert loaded.approval_policy == original.approval_policy
    binding = DeploymentBinding.model_validate(loaded.metadata["mcp_deployment"])
    policy = ServerPolicy.load(binding.policy_path, expected_sha256=binding.policy_sha256)
    target, = policy.targets.values()
    assert target.origin == "https://127.0.0.1:8443"
    assert policy.allowed_networks == ["127.0.0.1/32"]
    assert target.allowed_paths == paths and target.allow_all_paths == (mode == "site")
    validate_path(target, "/login")
    if mode == "paths":
        with pytest.raises(ToolInputError):
            validate_path(target, "/other")
    for path in Path(binding.policy_path).parent.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    before = {p: p.read_bytes() for p in Path(binding.policy_path).parent.iterdir()}
    assert synchronize_deployment(loaded, environment["REDTEAM_MCP_SERVERS"].split(","),
                                  env=environment) == loaded
    with environment_context({}):
        assert await prepare_web_deployment(store.path) == environment
    assert {p: p.read_bytes() for p in before} == before
    assert store.status()["draft"]["allowed_hosts"] == ["https://127.0.0.1:8443"]


async def test_automatic_selection_replaces_old_web_settings_without_mutating_process(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "lab.yaml")
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "old-web")
    monkeypatch.setenv("REDTEAM_MCP_OLD_WEB_KIND", "web_api")
    monkeypatch.setenv("REDTEAM_MCP_OLD_WEB_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_OLD_WEB_COMMAND", "missing-old-command")
    monkeypatch.setenv("REDTEAM_MCP_OLD_WEB_ARGS_JSON", '["--policy", "missing.json"]')
    before = dict(os.environ)
    environment = await prepare_web_deployment(store.path)
    assert environment["REDTEAM_MCP_SERVERS"] == "web-http,recon-passive"
    assert dict(os.environ) == before
    assert not (tmp_path / ".env").exists()


def test_preview_and_missing_confirmation_do_not_generate_files(tmp_path):
    store = EngagementStore(tmp_path / "lab.yaml")
    preview = store.preview(DRAFT)
    assert preview["summary"]["web_policy"]["mode"] == "site"
    assert not list(tmp_path.iterdir())
    with pytest.raises(EngagementError, match="confirmation_required"):
        store.save(DRAFT, confirm_scope=False, confirm_expansion=False)
    assert not list(tmp_path.iterdir())


def test_legacy_web_selection_encoding_omits_new_prepare_defaults():
    selection = WebPolicySelection.model_validate({
        "mode": "site", "origins": ["https://lab.test"],
    })
    assert selection.model_dump(mode="json") == {
        "version": 1, "mode": "site", "origins": ["https://lab.test"], "paths": [],
    }


def test_prepare_form_preview_and_save_are_pure_and_scope_bounded(tmp_path, monkeypatch):
    store = EngagementStore(tmp_path / "lab.yaml")

    def no_resolution(*_args, **_kwargs):
        raise AssertionError("DNS is startup-only")

    monkeypatch.setattr("asyncio.get_running_loop", no_resolution)
    preview = store.preview({**DRAFT, **PREPARE})
    assert preview["summary"]["web_policy"]["kali_network_mode"] == "prepare"
    assert preview["scope_expanded"] is False
    store.save({**DRAFT, **PREPARE}, confirm_scope=True, confirm_expansion=True)
    assert store.status()["draft"]["kali_sidecar_image"] == PREPARE["kali_sidecar_image"]
    assert not list(tmp_path.glob("*.mcp"))
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(("change", "field"), [
    ({"kali_endpoints": []}, "kali_endpoints"),
    ({"kali_limits_preset": "custom", "kali_limit_deliveries": 2},
     "kali_limits_preset"),
    ({"kali_sidecar_mode": "pinned", "kali_sidecar_image": None}, "kali_sidecar_image"),
    ({"kali_network_mode": "prepare", "kali_endpoints": [{
        "origin": "https://127.0.0.1:8443", "port": 9443, "transport": "tcp",
    }]}, "kali_endpoints"),
])
def test_prepare_input_errors_point_to_the_field_and_never_write(tmp_path, change, field):
    store = EngagementStore(tmp_path / "lab.yaml")
    with pytest.raises(EngagementError) as raised:
        store.save({**DRAFT, **PREPARE, **change}, confirm_scope=True, confirm_expansion=True)
    assert raised.value.problems[0]["field"] == field
    assert not list(tmp_path.iterdir())


async def test_prepare_startup_persists_selected_hash_and_overrides_ambient_gate(tmp_path):
    store = save(tmp_path / "lab.yaml", **PREPARE)
    process_before = env_snapshot()
    startup = await select_engagement_startup(
        store.path,
        base_environment={"REDTEAM_GATED_NETWORK_POLICY_PATH": "ambient.json"},
    )
    record = startup.engagement.metadata[GATED_PREPARATION_KEY]
    artifact = Path(record["artifact_path"])
    request = _load_prepared_policy(artifact)
    assert startup.environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] == str(artifact)
    assert startup.network_policy_hash == record["request_sha256"]
    assert policy_content_hash(request) == record["request_sha256"]
    assert request.sidecar_image == PREPARE["kali_sidecar_image"]
    assert "resolved_network_policy_hash" not in record
    before = artifact.read_bytes()
    second = await select_engagement_startup(
        store.path,
        base_environment={"REDTEAM_GATED_NETWORK_POLICY_PATH": "ambient.json"},
        require_recorded_preparation=True,
    )
    assert second.network_policy_hash == startup.network_policy_hash
    assert artifact.read_bytes() == before
    resumed = await select_engagement_startup(
        second.engagement,
        base_environment={"REDTEAM_GATED_NETWORK_POLICY_PATH": "ambient.json"},
        require_recorded_preparation=True,
    )
    assert Path(resumed.network_policy_path) == artifact
    assert resumed.network_policy_hash == startup.network_policy_hash
    assert artifact.read_bytes() == before
    assert env_snapshot() == process_before


async def test_managed_sidecar_selection_needs_no_shell_policy_path(tmp_path):
    store = save(
        tmp_path / "managed.yaml",
        **{**PREPARE, "kali_sidecar_mode": "managed", "kali_sidecar_image": None},
    )
    startup = await select_engagement_startup(store.path, base_environment={})
    request = _load_prepared_policy(Path(startup.network_policy_path))
    assert isinstance(request, GatedNetworkPolicyRequest)
    assert startup.environment.get("REDTEAM_SHELL_POLICY_PATH", "") == ""
    assert startup.network_policy_hash == policy_content_hash(request)


async def test_cli_build_passes_request_to_p_and_activates_only_p_resolved_policy(
    tmp_path, monkeypatch,
):
    store = save(
        tmp_path / "cli.yaml",
        **{**PREPARE, "kali_sidecar_mode": "managed", "kali_sidecar_image": None},
    )
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.delenv("REDTEAM_SHELL_POLICY_PATH", raising=False)
    config = load_app_config_for_run(str(store.path))
    prepared = {}
    captured = {}

    async def fake_prepare_shell_runtime(policy_path, **kwargs):
        selected = Path(kwargs["network_policy_path"])
        request = _load_prepared_policy(selected)
        assert isinstance(request, GatedNetworkPolicyRequest)
        payload = request.model_dump(mode="json")
        payload.pop("managed_sidecar")
        payload["sidecar_image"] = "example.invalid/gate@sha256:" + "b" * 64
        resolved = GatedNetworkPolicy.model_validate(payload)
        prepared.update(
            shell_policy_path=policy_path,
            request_hash=policy_content_hash(request),
            resolved=resolved,
        )
        return SimpleNamespace(
            preparation_status="network_prerequisites_ready",
            network_policy=resolved,
            network_policy_hash=policy_content_hash(request),
            adapter=object(),
            concurrency={},
            output_limits={},
            program_source=None,
            environment_fingerprint=None,
        )

    async def fake_build_application(app_config, **kwargs):
        captured["config"] = app_config
        captured["kwargs"] = kwargs
        return object(), SimpleNamespace()

    monkeypatch.setattr(run_command, "prepare_shell_runtime", fake_prepare_shell_runtime)
    monkeypatch.setattr(run_command, "build_application", fake_build_application)
    process_environment = env_snapshot()
    await run_command.build(config, max_steps=1, engagement_path=store.path)

    assert prepared["shell_policy_path"] is None
    assert prepared["request_hash"] != policy_content_hash(prepared["resolved"])
    runtime, provider = captured["kwargs"]["gated_shell"]
    assert provider.network_policy is prepared["resolved"]
    assert runtime.network_policy_hash == prepared["request_hash"]
    assert captured["config"].engagement.metadata[GATED_PREPARATION_KEY]["request_sha256"] \
        == prepared["request_hash"]
    assert env_snapshot() == process_environment


async def test_prepare_resume_rejects_environment_and_dns_drift_without_rewriting(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "lab.yaml", **{
        **PREPARE,
        "allowed_hosts": ["https://lab.test:8443"],
        "kali_endpoints": [{"origin": "https://lab.test:8443", "port": 8443,
                            "transport": "tcp"}],
        "kali_docker_context": None,
    })
    calls = 0

    async def dns(_host, port, **_kwargs):
        nonlocal calls
        calls += 1
        address = "192.0.2.7" if calls <= 2 else "192.0.2.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    import socket

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    first = await select_engagement_startup(store.path, base_environment={"DOCKER_CONTEXT": "one"})
    artifact = Path(first.network_policy_path)
    before = artifact.read_bytes()
    with pytest.raises(ValueError, match="kali_preparation_selection_or_environment_changed"):
        await select_engagement_startup(
            store.path, base_environment={"DOCKER_CONTEXT": "two"},
            require_recorded_preparation=True,
        )
    with pytest.raises(ValueError, match="kali_preparation_dns_or_selection_changed"):
        await select_engagement_startup(
            store.path, base_environment={"DOCKER_CONTEXT": "one"},
            require_recorded_preparation=True,
        )
    assert artifact.read_bytes() == before


async def test_closed_clears_ambient_and_inherit_keeps_it(tmp_path):
    closed = save(tmp_path / "closed.yaml", kali_network_mode="closed")
    inherited = save(tmp_path / "inherited.yaml", kali_network_mode="inherit")
    ambient = {"REDTEAM_GATED_NETWORK_POLICY_PATH": "operator-policy.json"}
    closed_selection = await select_engagement_startup(closed.path, base_environment=ambient)
    inherited_selection = await select_engagement_startup(
        inherited.path, base_environment=ambient,
    )
    assert closed_selection.environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] == ""
    assert inherited_selection.environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] \
        == "operator-policy.json"


async def test_configured_network_policy_remains_hash_bound(tmp_path):
    policy = build_gated_network_policy(
        targets={"127.0.0.1": ["127.0.0.1"]},
        sidecar=PREPARE["kali_sidecar_image"],
        preset="probe",
        endpoints={"127.0.0.1": [(8443, "tcp")]},
    )
    assert isinstance(policy, GatedNetworkPolicy)
    selected_path = tmp_path / "operator-policy.json"
    selected_path.write_text(json.dumps(policy.model_dump(mode="json")), encoding="utf-8")
    store = save(
        tmp_path / "configured.yaml", kali_network_mode="configured",
        kali_network_policy_path=str(selected_path),
    )
    startup = await select_engagement_startup(store.path)
    copied = Path(startup.network_policy_path)
    expected = store.load().metadata["web_policy"]["kali_network_policy_sha256"]
    assert startup.network_policy_hash == expected
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == expected
    assert copied.read_bytes() == selected_path.read_bytes()
    assert startup.environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] == str(copied)


@pytest.mark.parametrize("change", [
    {"web_policy_mode": "paths", "web_paths": []},
    {"web_policy_mode": "paths", "web_paths": ["//other.host"]},
    {"web_policy_mode": "paths", "web_paths": ["/a/../private"]},
    {"web_policy_mode": []},
    {"allowed_hosts": ["https://user:password@127.0.0.1"]},
])
def test_bad_policy_input_is_rejected_before_writing(tmp_path, change):
    store = EngagementStore(tmp_path / "lab.yaml")
    with pytest.raises(EngagementError):
        store.save({**DRAFT, **change}, confirm_scope=True, confirm_expansion=True)
    assert not list(tmp_path.iterdir())


async def test_path_expansion_requires_confirmation_and_preserves_old_artifacts(tmp_path):
    store = save(tmp_path / "lab.yaml", web_policy_mode="paths", web_paths=["/"])
    with environment_context({}):
        await prepare_web_deployment(store.path)
    original = store.load()
    old = Path(original.metadata["mcp_deployment"]["policy_path"])
    old_bytes = old.read_bytes()
    for changed in ({**DRAFT}, {**DRAFT, "web_policy_mode": "paths", "web_paths": ["/", "/api"]}):
        assert store.preview(changed)["scope_expanded"]
        with pytest.raises(EngagementError, match="confirmation_required"):
            store.save(changed, confirm_scope=True, confirm_expansion=False)
        assert store.load() == original
    store.save(DRAFT, confirm_scope=True, confirm_expansion=True)
    with environment_context({}):
        await prepare_web_deployment(store.path)
    new = Path(store.load().metadata["mcp_deployment"]["policy_path"])
    assert new != old and old.read_bytes() == old_bytes
    assert ServerPolicy.load(str(new)).targets != ServerPolicy.load(str(old)).targets


async def test_corrupt_generated_policy_is_rejected_without_overwrite(tmp_path):
    store = save(tmp_path / "lab.yaml")
    with environment_context({}):
        await prepare_web_deployment(store.path)
    target = Path(store.load().metadata["mcp_deployment"]["policy_path"])
    target.write_text("{}")
    with environment_context({}), pytest.raises(ValueError, match="deployment validation failed"):
        await prepare_web_deployment(store.path)
    assert target.read_text() == "{}"


async def test_session_environments_and_policy_files_remain_independent(tmp_path):
    stores = [save(tmp_path / f"lab-{i}.yaml", allowed_hosts=[f"http://127.0.0.1:{port}"])
              for i, port in enumerate((8080, 8081))]
    before = env_snapshot()
    async def selected(store):
        launcher = RealLauncher(store.path)
        await launcher.prepare()
        with environment_context(launcher.environment):
            await asyncio.sleep(0)
            server_args = json.loads(env_snapshot()["REDTEAM_MCP_WEB_HTTP_ARGS_JSON"])
            assert str(store.path.stem) in server_args[4]
            assert env_snapshot({}) == {}
            return server_args[4]
    left, right = await asyncio.gather(*(selected(store) for store in stores))
    assert left != right and env_snapshot() == before


async def test_hostname_dns_is_pinned_and_not_refreshed_on_restart(tmp_path, monkeypatch):
    store = save(tmp_path / "lab.yaml", allowed_hosts=["https://lab.test"])
    calls = []
    async def dns(host, port, **kwargs):
        calls.append((host, port))
        return [(2, 1, 6, "", ("192.0.2.7", port))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    with environment_context({}):
        await prepare_web_deployment(store.path)
        await prepare_web_deployment(store.path)
    assert calls == [("lab.test", 443)]
    spec = DeploymentSpec.model_validate_json(Path(
        store.load().metadata["mcp_deployment"]["source_path"],
    ).read_bytes())
    assert spec.policy.allowed_networks == ["192.0.2.7/32"]


async def test_manual_selection_does_not_create_policy_or_replace_servers(tmp_path):
    store = save(tmp_path / "lab.yaml", web_policy_mode="manual")
    with environment_context({"REDTEAM_MCP_SERVERS": "custom"}):
        assert await prepare_web_deployment(store.path) == {"REDTEAM_MCP_SERVERS": "custom"}
    assert not list(tmp_path.glob("*.mcp"))
    assert "mcp_deployment" not in load_engagement_config(store.path).metadata


@pytest.mark.parametrize("path", ["//other.host/a", "https://other.host/a", "/a/../b", "/a%2fb"])
def test_whole_site_permission_still_rejects_url_authority_and_ambiguous_paths(path):
    target = TargetProfile(origin="https://lab.test", allow_all_paths=True)
    with pytest.raises(ToolInputError):
        validate_path(target, path)


def test_old_exact_path_policy_encoding_remains_compatible():
    source = {"origin": "https://lab.test", "allowed_paths": ["/"]}
    target = TargetProfile.model_validate(source)
    assert "allow_all_paths" not in target.model_dump()
    assert not target.allows_path("/login")
    with pytest.raises(ValidationError):
        TargetProfile(origin="https://lab.test", allowed_paths=[])


async def test_initial_build_uses_mcp_ids_from_composed_startup_environment(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "lab-composed-ids.yaml", kali_network_mode="closed")
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    config = load_app_config_for_run(str(store.path))
    assert "web-http" not in config.capability.mcp_servers
    captured = {}

    async def no_shell_runtime(policy_path, **kwargs):
        return None

    async def fake_build_application(app_config, **kwargs):
        captured["mcp_servers"] = list(app_config.capability.mcp_servers)
        return object(), SimpleNamespace()

    monkeypatch.setattr(run_command, "prepare_shell_runtime", no_shell_runtime)
    monkeypatch.setattr(run_command, "build_application", fake_build_application)
    await run_command.build(config, max_steps=1, engagement_path=store.path)

    assert "web-http" in captured["mcp_servers"]


class _NoInputFrontend(ConsoleFrontend):
    """Single-run frontend: the objective is supplied by the caller and no line is read."""

    async def read_line(self) -> str:
        raise EOFError


def _patch_built_none_runtime(monkeypatch, build_hook=None):
    executed = []

    async def fake_prepare_shell_runtime(policy_path, **kwargs):
        request = _load_prepared_policy(Path(kwargs["network_policy_path"]))
        assert isinstance(request, GatedNetworkPolicyRequest)
        payload = request.model_dump(mode="json")
        payload.pop("managed_sidecar")
        payload["sidecar_image"] = "example.invalid/gate@sha256:" + "b" * 64
        return SimpleNamespace(
            preparation_status="network_prerequisites_ready",
            network_policy=GatedNetworkPolicy.model_validate(payload),
            network_policy_hash=policy_content_hash(request),
            adapter=object(),
            concurrency={},
            output_limits={},
            program_source=None,
            environment_fingerprint=None,
        )

    async def fake_build_application(app_config, **kwargs):
        if build_hook is not None:
            build_hook()
        return object(), SimpleNamespace()

    async def fake_execute(engagement, deps, **kwargs):
        executed.append(engagement)
        raise RuntimeError("sentinel after startup")

    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.delenv("REDTEAM_SHELL_POLICY_PATH", raising=False)
    monkeypatch.setattr(run_command, "prepare_shell_runtime", fake_prepare_shell_runtime)
    monkeypatch.setattr(run_command, "build_application", fake_build_application)
    monkeypatch.setattr(run_command, "execute", fake_execute)
    return executed


async def _run_session_without_prebuilt_runtime(store):
    from rich.console import Console

    config = load_app_config_for_run(str(store.path))
    session = InteractiveSession(config.engagement, config.llm, engagement_path=store.path)
    return await run_session(
        Console(file=io.StringIO()),
        config,
        conversation=None,
        session=session,
        objective="startup regression",
        frontend=_NoInputFrontend(Console(file=io.StringIO())),
        single_run=True,
    )


def _operator_edit(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    edited = text.replace(DRAFT["name"], "operator edit")
    assert edited != text
    path.write_text(edited, encoding="utf-8")
    assert load_engagement_config(path).name == "operator edit"


UNPREPARED_MANAGED = {**PREPARE, "kali_sidecar_mode": "managed", "kali_sidecar_image": None}


async def test_built_none_unprepared_startup_writes_are_not_operator_drift(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "unprepared.yaml", **UNPREPARED_MANAGED)
    before = store.path.read_bytes()
    executed = _patch_built_none_runtime(monkeypatch)

    result = await _run_session_without_prebuilt_runtime(store)

    assert store.path.read_bytes() != before
    assert GATED_PREPARATION_KEY in store.load().metadata
    assert len(executed) == 1
    assert "sentinel after startup" in (result.error or "")
    assert "설정이 변경되어" not in (result.error or "")


async def test_built_none_operator_edit_during_build_is_drift_and_never_dispatches(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "drift.yaml", **UNPREPARED_MANAGED)
    executed = _patch_built_none_runtime(
        monkeypatch, build_hook=lambda: _operator_edit(store.path),
    )

    result = await _run_session_without_prebuilt_runtime(store)

    assert executed == []
    assert result.exit_code == 2
    assert "설정이 변경되어 시작하지 않았습니다" in (result.error or "")
    assert store.load().name == "operator edit"


async def test_built_none_operator_edit_in_install_window_refuses_install_without_overwrite(
    tmp_path, monkeypatch,
):
    store = save(tmp_path / "install-window.yaml", **UNPREPARED_MANAGED)
    original_artifact = web_policy_module._atomic_policy_artifact

    def edit_after_artifact(root, policy):
        written = original_artifact(root, policy)
        _operator_edit(store.path)
        return written

    monkeypatch.setattr(web_policy_module, "_atomic_policy_artifact", edit_after_artifact)
    executed = _patch_built_none_runtime(monkeypatch)

    result = await _run_session_without_prebuilt_runtime(store)

    assert executed == []
    assert result.exit_code != 0
    assert "EngagementError" in (result.error or "")
    loaded = store.load()
    assert loaded.name == "operator edit"
    assert GATED_PREPARATION_KEY not in loaded.metadata
