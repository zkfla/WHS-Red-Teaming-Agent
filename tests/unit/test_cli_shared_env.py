"""``_shared.load_dotenv_if_present`` — the CLI's best-effort ``.env`` loader.

A real shell/CI export must always win over a stale ``.env`` value, and a
missing/malformed file must never be fatal — both properties are load-bearing
(the interactive setup wizard writes secrets/LLM/MCP settings into ``.env``
and relies on this loader to make them take effect without a manual
``export``/``source`` step) but weren't covered by any single test before.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from redteam.app.cli._shared import load_dotenv_if_present


@pytest.fixture(autouse=True)
def isolate_dotenv_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loader writes directly, outside monkeypatch tracking. delenv() on
    # an absent key alone cannot restore a key subsequently created by it.
    # Protect all later integration tests from fixture MCP/LLM configuration.
    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_missing_file_is_a_silent_no_op(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_PROVIDER", raising=False)
    load_dotenv_if_present(tmp_path / "does-not-exist.env")
    assert "REDTEAM_LLM_PROVIDER" not in os.environ


def test_loads_a_simple_key_value_pair(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_PROVIDER", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("REDTEAM_LLM_PROVIDER=fake\n", encoding="utf-8")

    load_dotenv_if_present(env_path)

    assert os.environ["REDTEAM_LLM_PROVIDER"] == "fake"


def test_skips_blank_lines_and_comments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n# a comment\n   \nREDTEAM_LLM_MODEL=gpt-4o-mini\n# REDTEAM_LLM_MODEL=ignored\n",
        encoding="utf-8",
    )

    load_dotenv_if_present(env_path)

    assert os.environ["REDTEAM_LLM_MODEL"] == "gpt-4o-mini"


def test_strips_one_layer_of_matching_quotes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REDTEAM_MCP_WEB_EXAMPLE_ALLOWED_TOOLS_JSON", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        'REDTEAM_MCP_WEB_EXAMPLE_ALLOWED_TOOLS_JSON=\'["session_list"]\'\n', encoding="utf-8"
    )

    load_dotenv_if_present(env_path)

    assert os.environ["REDTEAM_MCP_WEB_EXAMPLE_ALLOWED_TOOLS_JSON"] == '["session_list"]'


def test_a_real_environment_variable_always_wins_over_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    """The load-bearing property: an explicit shell/CI export is never
    silently overwritten by a stale value left in `.env`."""
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    env_path = tmp_path / ".env"
    env_path.write_text("REDTEAM_LLM_PROVIDER=fake\n", encoding="utf-8")

    load_dotenv_if_present(env_path)

    assert os.environ["REDTEAM_LLM_PROVIDER"] == "litellm"


def test_malformed_lines_are_skipped_not_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "this line has no equals sign\nREDTEAM_LLM_MODEL=gpt-4o-mini\n", encoding="utf-8"
    )

    load_dotenv_if_present(env_path)  # must not raise

    assert os.environ["REDTEAM_LLM_MODEL"] == "gpt-4o-mini"
