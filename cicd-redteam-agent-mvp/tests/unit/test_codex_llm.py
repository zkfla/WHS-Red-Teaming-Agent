from pathlib import Path

from cicd_redteam_agent.framework.codex_llm import _isolated_config_overrides


def test_codex_inference_runtime_disables_plugins_and_configured_mcp_servers(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.chrome-devtools]
command = "npx"

[mcp_servers.semgrep]
command = "semgrep"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    overrides = _isolated_config_overrides()

    assert overrides == (
        "features.plugins=false",
        "mcp_servers.chrome-devtools.enabled=false",
        "mcp_servers.semgrep.enabled=false",
    )
