"""Confirmed web selections and scoped MCP assembly; temporary files only."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from redteam.app.mcp_deployment import DeploymentBinding, synchronize_deployment
from redteam.app.web.engagement import EngagementError, EngagementStore
from redteam.app.web.host import RealLauncher
from redteam.app.web_policy import prepare_web_deployment
from redteam.capability.deployment import DeploymentSpec
from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.servers.network import ToolInputError, validate_path
from redteam.core.config import load_engagement_config
from redteam.core.environment import env_snapshot, environment_context

DRAFT = {"name": "local lab", "allowed_hosts": ["https://127.0.0.1:8443"],
         "allowed_capabilities": ["web"], "max_actions": 5, "web_policy_mode": "site"}


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
