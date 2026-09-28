"""Executable checks for the operator-run external Web MCP profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redteam.capability.settings import MCPServerSettings
from redteam.orchestration.action_naming import canonical_action_key

ROOT = Path(__file__).parents[2]
PROFILE_DIRECTORY = ROOT / "deploy" / "mcp" / "external"

PROFILE_SERVERS = {
    "zap.env.example": ("zap-recon", "zap-web"),
    "burp.env.example": ("burp-observe", "burp-oast"),
    "chrome-use.env.example": ("chrome-use",),
    "cua.env.example": ("cua-driver",),
}


def _environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[name] = value
    return values


@pytest.mark.parametrize(("profile", "server_ids"), PROFILE_SERVERS.items())
def test_external_profile_is_a_complete_fail_closed_configuration(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    server_ids: tuple[str, ...],
) -> None:
    for name, value in _environment(PROFILE_DIRECTORY / profile).items():
        monkeypatch.setenv(name, value)

    for server_id in server_ids:
        settings = MCPServerSettings.from_environment(server_id)
        assert settings.allowed_tools is not None
        assert settings.tool_operations is not None
        assert set(settings.allowed_tools) == set(settings.tool_operations)
        for operations in settings.tool_operations.values():
            for operation in operations:
                canonical_action_key(settings.kind.value.replace("_api", ""), operation)


def test_zap_profile_sends_the_exact_security_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _environment(PROFILE_DIRECTORY / "zap.env.example").items():
        monkeypatch.setenv(name, value)

    settings = MCPServerSettings.from_environment("zap-web")

    assert settings.request_headers() == {
        "Authorization": "replace-with-operator-managed-key"
    }


def test_external_app_only_references_documented_profiles() -> None:
    config = yaml.safe_load(
        (ROOT / "examples" / "external-web-app.yaml").read_text(encoding="utf-8")
    )

    assert config["capability"]["mcp_servers"] == [
        "zap-recon",
        "zap-web",
        "burp-observe",
        "chrome-use",
        "cua-driver",
    ]

