"""Operator input through shared CLI assembly; preparation uses explicit test doubles."""

import json
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock

import pytest

from redteam.app import kali_preparation as app
from redteam.app.cli.commands import run
from redteam.app.cli.tool_context import configured_tool_context
from redteam.app.web.engagement import EngagementError, field_definitions
from redteam.app.web_policy import WebPolicySelection, prepare_web_deployment
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.direct_target import (
    DirectTargetConfig,
    DirectTargetPreparationError,
    DirectTargetProviderConfig,
    PreparedTargetNetwork,
    TargetNetworkPreparation,
    configure_direct_target,
)
from redteam.capability.shell_runtime import build_shell_runtime
from redteam.core.config import AppConfig
from redteam.core.environment import env_snapshot, environment_context
from redteam.orchestration.component_binding import builtin_component_binding
from tests.unit.test_web_permissions import gated_config
from tests.unit.test_web_policy import DRAFT, save

FINGERPRINT = "ab" * 32
IMAGE = "sha256:" + "cd" * 32


@dataclass
class AssemblyDeps:
    component_binding: object


def shell(tmp_path):
    path = tmp_path / "shell.json"
    path.write_text(json.dumps({"image": IMAGE, "docker_context": "selected"}))
    return build_shell_runtime(str(path))


async def test_form_preview_save_and_idle_inspection_do_not_prepare(tmp_path, monkeypatch):
    prepare = AsyncMock()
    monkeypatch.setattr(app, "prepare_target_network", prepare)
    store = save(tmp_path / "lab.yaml")
    change = {**DRAFT, "kali_network_mode": "prepare"}
    preview = store.preview(change)
    assert preview["scope_expanded"]
    config = preview["summary"]["web_policy"]["kali_network_preparation"]
    assert config == TargetNetworkPreparation().model_dump()
    with pytest.raises(EngagementError, match="confirmation_required"):
        store.save(change, confirm_scope=True, confirm_expansion=False)
    store.save(change, confirm_scope=True, confirm_expansion=True)
    assert store.status()["draft"]["kali_network_preparation"] == config
    definition = next(
        row for row in field_definitions() if row["name"] == "kali_network_preparation"
    )
    assert definition["default"] == config
    with environment_context({"REDTEAM_GATED_NETWORK_POLICY_PATH": "missing.json"}):
        before = env_snapshot()
        selected = await prepare_web_deployment(store.path)
        assert env_snapshot() == before
        assert selected["REDTEAM_GATED_NETWORK_POLICY_PATH"] == ""
        context = configured_tool_context(AppConfig(engagement=store.load()))
        assert context["tool_availability"]["shell"]["target_network"] == "prepare_at_run_start"
    prepare.assert_not_awaited()


def test_bad_preparation_and_missing_policy_report_input_fields(tmp_path):
    store = save(tmp_path / "lab.yaml")
    for values in ({"docker_context": "bad context"}, {"target_network_id": "short"}):
        with pytest.raises(EngagementError) as failed:
            store.preview(
                {**DRAFT, "kali_network_mode": "prepare", "kali_network_preparation": values}
            )
        assert any(
            row["field"].startswith("kali_network_preparation") for row in failed.value.problems
        )
    with pytest.raises(EngagementError) as failed:
        store.preview(
            {
                **DRAFT,
                "kali_network_mode": "configured",
                "kali_network_policy_path": str(tmp_path / "missing.json"),
            }
        )
    assert failed.value.problems == [
        {"field": "kali_network_policy_path", "code": "kali_policy_invalid"}
    ]


def test_old_gate_fields_are_dropped_during_selection_migration():
    prepared = TargetNetworkPreparation.model_validate({
        "docker_context": "selected",
        "sidecar_image": "old-sidecar",
        "max_output_bytes": 1024,
        "duration_seconds": 60,
    })
    assert prepared.model_dump() == {
        "docker_context": "selected", "target_network_id": None,
    }


@pytest.mark.parametrize("web_mode", ["site", "manual"])
async def test_explicit_closed_wins_over_invalid_environment(tmp_path, monkeypatch, web_mode):
    store = save(tmp_path / "lab.yaml", web_policy_mode=web_mode, kali_network_mode="closed")
    prepare = AsyncMock()
    monkeypatch.setattr(app, "prepare_target_network", prepare)
    with environment_context({"REDTEAM_GATED_NETWORK_POLICY_PATH": "missing.json"}):
        environment = await prepare_web_deployment(store.path)
        assert environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] == ""
        assert await app.prepare_kali_network(store.load(), None) is None
        with pytest.raises(DirectTargetPreparationError,
                           match="direct_target_preparation_changed"):
            await app.prepare_kali_network(store.load(), None, expected_fingerprint=FINGERPRINT)
    assert store.status()["summary"]["kali_network_mode"] == "closed"
    prepare.assert_not_awaited()


async def test_prepare_selection_reaches_direct_target_catalog(tmp_path, monkeypatch):
    store = save(
        tmp_path / "lab.yaml",
        kali_network_mode="prepare",
        kali_network_preparation={"docker_context": "selected"},
    )
    runtime = shell(tmp_path)

    async def prepared(policy, network, **kwargs):
        assert policy is runtime.policy
        assert network.targets == {"127.0.0.1": ("127.0.0.1",)}
        assert network.target_ports == {"127.0.0.1": (8443,)}
        assert kwargs["expected_fingerprint"] == FINGERPRINT
        return PreparedTargetNetwork(
            DirectTargetProviderConfig(network, {}, {}), FINGERPRINT, "selected",
        )

    prepare = AsyncMock(side_effect=prepared)
    monkeypatch.setattr(app, "prepare_target_network", prepare)
    result = await app.prepare_kali_network(
        store.load(), runtime, expected_fingerprint=FINGERPRINT
    )
    assert result.fingerprint == FINGERPRINT
    _, _, descriptors = configure_direct_target(runtime, store.load().scope, result.provider)
    assert {row.tool_name for row in descriptors} == {"shell_target_run"}
    gateway = await build_mcp_tool_gateway(
        [],
        builtin_adapters=[runtime.adapter],
        builtin_output_limits=runtime.output_limits,
        health_refresh_enabled=False,
    )
    assert {row.tool_name for row in gateway.all()} >= {"shell_target_run", "shell_run"}
    assert "shell_target_job_start" not in {row.tool_name for row in gateway.all()}
    prepare.assert_awaited_once()


async def test_configured_policy_pin_mismatch_is_not_silently_inherited(tmp_path, monkeypatch):
    source = tmp_path / "gate.json"
    values = gated_config()
    values["registry"]["targets"]["127.0.0.1"] = ["192.0.2.4"]
    source.write_text(json.dumps(values))
    store = save(
        tmp_path / "lab.yaml",
        allowed_networks=["0.0.0.0/0"],
        kali_network_mode="configured",
        kali_network_policy_path=str(source),
    )
    prepare = AsyncMock()
    monkeypatch.setattr(app, "prepare_target_network", prepare)
    with pytest.raises(DirectTargetPreparationError, match="direct_target_pin_mismatch"):
        await app.prepare_kali_network(store.load(), shell(tmp_path))
    prepare.assert_not_awaited()


@pytest.mark.parametrize("resuming", [False, True])
async def test_run_passes_context_and_fingerprints_without_a_second_shell(
    tmp_path, monkeypatch, resuming
):
    store = save(
        tmp_path / "lab.yaml",
        kali_network_mode="prepare",
        kali_network_preparation={"docker_context": "selected"},
    )
    runtime = replace(shell(tmp_path), environment_fingerprint="ef" * 32)
    network = DirectTargetConfig(
        targets={"127.0.0.1": ("127.0.0.1",)},
        target_ports={"127.0.0.1": (80,)},
    )
    prepared = PreparedTargetNetwork(
        DirectTargetProviderConfig(network, {}, {}), FINGERPRINT, "selected"
    )
    shell_prepare = AsyncMock(return_value=runtime)
    gate_prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(run, "prepare_shell_runtime", shell_prepare)
    monkeypatch.setattr(app, "prepare_kali_network", gate_prepare)
    deps = AssemblyDeps(builtin_component_binding())
    build = AsyncMock(return_value=("engagement", deps))
    monkeypatch.setattr(run, "build_application", build)
    options = {}
    if resuming:
        options = {
            "original_stores": {"evidence": "e", "audit": "a", "approvals": "p"},
            "original_component_binding": builtin_component_binding().model_copy(
                update={
                    "execution_environment_fingerprint": runtime.environment_fingerprint,
                    "network_environment_fingerprint": FINGERPRINT,
                }
            ),
        }
    with environment_context({"REDTEAM_SHELL_AUTO_SETUP": "true"}):
        _, result = await run.build(AppConfig(engagement=store.load()), max_steps=1, **options)
    assert gate_prepare.await_args.kwargs["expected_fingerprint"] == (
        FINGERPRINT if resuming else None
    )
    assert shell_prepare.await_args.kwargs["docker_context"] == "selected"
    assert build.await_args.kwargs["direct_target_shell"] == (runtime, prepared.provider)
    assert build.await_args.kwargs["builtin_adapters"][0] is runtime.adapter
    assert result.component_binding.network_environment_fingerprint == FINGERPRINT
    assert (
        result.component_binding.execution_environment_fingerprint
        == runtime.environment_fingerprint
    )
    build.reset_mock()
    gate_prepare.side_effect = DirectTargetPreparationError(
        "direct_target_route_unavailable", "target_network_id", "Select the target bridge."
    )
    with environment_context({}), pytest.raises(DirectTargetPreparationError):
        await run.build(AppConfig(engagement=store.load()), max_steps=1)
    build.assert_not_awaited()


def test_optional_network_identity_omits_legacy_field():
    original = builtin_component_binding()
    assert "network_environment_fingerprint" not in original.model_dump(mode="json")
    updated = original.model_copy(update={"network_environment_fingerprint": FINGERPRINT})
    assert updated.model_dump(mode="json")["network_environment_fingerprint"] == FINGERPRINT


def test_old_selection_encoding_does_not_gain_preparation_defaults():
    selection = WebPolicySelection(mode="site", origins=DRAFT["allowed_hosts"])
    assert "kali_network_preparation" not in selection.model_dump(mode="json")


async def test_configured_network_port_must_match_the_selected_origin(tmp_path, monkeypatch):
    source = tmp_path / "gate.json"
    values = gated_config()
    values["target_ports"] = {"127.0.0.1": [9090]}
    source.write_text(json.dumps(values))
    store = save(
        tmp_path / "lab.yaml", kali_network_mode="configured", kali_network_policy_path=str(source)
    )
    prepare = AsyncMock()
    monkeypatch.setattr(app, "prepare_target_network", prepare)
    with pytest.raises(DirectTargetPreparationError, match="direct_target_port_mismatch"):
        await app.prepare_kali_network(store.load(), shell(tmp_path))
    prepare.assert_not_awaited()
