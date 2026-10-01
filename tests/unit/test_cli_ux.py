"""CLI UX contract: help text, exit codes, invalid command, secret masking.

Subprocess-based on purpose -- this is exactly what a real invocation of
`redteam ...`/`python -m redteam.app.cli ...` does, including argparse's
own SystemExit(2) for a bad command.
"""

from __future__ import annotations

import subprocess
import sys


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "redteam.app.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        input="",
        timeout=20,
        check=False,
    )


def test_help_lists_every_v1_command() -> None:
    result = _cli("--help")
    assert result.returncode == 0
    for command in (
        "init",
        "validate",
        "run",
        "status",
        "roe",
        "tools",
        "approvals",
        "findings",
        "report",
    ):
        assert command in result.stdout


def test_run_help_documents_non_interactive() -> None:
    result = _cli("run", "--help")
    assert result.returncode == 0
    assert "--non-interactive" in result.stdout


def test_tools_help_works() -> None:
    result = _cli("tools", "--help")
    assert result.returncode == 0
    assert "list" in result.stdout
    assert "health" in result.stdout


def test_approvals_help_works() -> None:
    result = _cli("approvals", "--help")
    assert result.returncode == 0
    assert "approve" in result.stdout
    assert "deny" in result.stdout


def test_invalid_command_exits_nonzero_with_usage_on_stderr() -> None:
    result = _cli("not-a-real-command")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_no_command_exits_nonzero() -> None:
    result = _cli()
    assert result.returncode == 2


def test_validate_missing_file_is_a_config_error_not_a_traceback() -> None:
    result = _cli("validate", "/nonexistent/engagement.yaml")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_tools_list_json_is_valid_json() -> None:
    import json

    result = _cli("tools", "list", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
