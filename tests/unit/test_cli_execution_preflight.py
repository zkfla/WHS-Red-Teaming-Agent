"""Offline operator settings checks are not live readiness or admission."""

import json
from pathlib import Path

import pytest

from redteam.app.cli.commands import validate
from redteam.capability.shell_workroom import ShellWorkroomManager
from tests.unit.test_cli_validate import _args, _write
from tests.unit.test_shell_runtime_008 import _gated_policy_file, _policy_file


@pytest.fixture
def engagement(tmp_path, monkeypatch):
    for key in ("REDTEAM_MCP_SERVERS", "REDTEAM_LLM_PROVIDER", "REDTEAM_SPECIALIST_PROFILE",
                "REDTEAM_LLM_REQUEST_POLICY_JSON", "REDTEAM_CODE_FIXTURE_POLICY_PATH",
                "REDTEAM_SHELL_POLICY_PATH", "REDTEAM_GATED_NETWORK_POLICY_PATH"):
        monkeypatch.delenv(key, raising=False)
    return _write(tmp_path, "engagement.yaml", """
name: preflight
scope:
  allowed_hosts: ["test.invalid"]
execution_budget:
  max_actions: 1
""")


async def test_missing_shell_policy_is_rejected_before_execution(engagement, monkeypatch):
    monkeypatch.setenv("REDTEAM_SHELL_POLICY_PATH", str(engagement.parent / "absent.json"))
    assert await validate.run(_args(engagement, json_mode=True)) == 2


async def test_gate_without_shell_is_rejected(engagement, monkeypatch):
    monkeypatch.setenv("REDTEAM_GATED_NETWORK_POLICY_PATH", _gated_policy_file(engagement.parent))
    assert await validate.run(_args(engagement, json_mode=True)) == 2


async def test_specialist_profile_requires_common_request_policy(engagement, monkeypatch):
    profile = Path(__file__).resolve().parents[2] / "config/agents/native-specialists-web.yaml"
    monkeypatch.setenv("REDTEAM_SPECIALIST_PROFILE", str(profile))
    assert await validate.run(_args(engagement, json_mode=True)) == 2


async def test_fixture_without_selected_code_roles_is_rejected(engagement, monkeypatch):
    monkeypatch.setenv("REDTEAM_CODE_FIXTURE_POLICY_PATH", _policy_file(engagement.parent))
    assert await validate.run(_args(engagement, json_mode=True)) == 2


async def test_complete_settings_are_checked_without_docker_or_model(
    engagement, monkeypatch, capsys,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline validation must not run Docker or build an LLM")
    monkeypatch.setattr(ShellWorkroomManager, "_run_docker", forbidden)
    monkeypatch.setattr("redteam.app.bootstrap.build_llm_client", forbidden)
    profile = Path(__file__).resolve().parents[2] / "config/agents/native-specialists-web.yaml"
    shell = _policy_file(engagement.parent)
    monkeypatch.setenv("REDTEAM_SPECIALIST_PROFILE", str(profile))
    monkeypatch.setenv("REDTEAM_LLM_REQUEST_POLICY_JSON", json.dumps({
        "mode": "bounded", "max_requests": 100, "max_concurrent_requests": 2,
    }))
    monkeypatch.setenv("REDTEAM_SHELL_POLICY_PATH", shell)
    monkeypatch.setenv("REDTEAM_GATED_NETWORK_POLICY_PATH", _gated_policy_file(engagement.parent))
    monkeypatch.setenv("REDTEAM_CODE_FIXTURE_POLICY_PATH", shell)
    assert await validate.run(_args(engagement, json_mode=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert {"Shell policy", "Gated network policy", "Specialist profile",
            "Offline fixture policy"} <= names
    assert payload["live_readiness_checked"] is False
    assert payload["admission_checked"] is False


async def test_policy_error_does_not_echo_values(engagement, monkeypatch, capsys):
    shell = _policy_file(engagement.parent, max_jobs="S3CR3T-POLICY-VALUE")
    monkeypatch.setenv("REDTEAM_SHELL_POLICY_PATH", shell)
    assert await validate.run(_args(engagement, json_mode=True)) == 2
    output = capsys.readouterr().out
    assert "S3CR3T-POLICY-VALUE" not in output and "max_jobs" in output
