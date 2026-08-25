"""credentials.py 의 순수 로직만 검증한다 — 실제 대화형 프롬프트(Prompt.ask)는
사람 입력이 필요하므로 여기서 트리거하지 않는다. 환경변수 우선순위와
저장/조회 라운드트립, 비대화형일 때 실패하는 경로만 확인한다."""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from finredteam import credentials


@pytest.fixture(autouse=True)
def _isolated_cred_store(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_CRED_DIR", tmp_path / ".finredteam")
    monkeypatch.setattr(credentials, "_CRED_PATH", tmp_path / ".finredteam" / "credentials.json")


def test_mask_short_key_is_all_stars():
    assert credentials.mask("short") == "*****"


def test_mask_long_key_keeps_prefix_and_suffix():
    masked = credentials.mask("sk-ant-abcdefghijklmnop1234")
    assert masked.startswith("sk-ant")
    assert masked.endswith("1234")
    assert "…" in masked


def test_add_key_and_list_keys_round_trip():
    assert credentials.list_keys("anthropic") == []
    credentials.add_key("anthropic", "personal", "sk-ant-xxx")
    entries = credentials.list_keys("anthropic")
    assert entries == [{"label": "personal", "key": "sk-ant-xxx"}]


def test_add_key_with_same_label_overwrites():
    credentials.add_key("anthropic", "personal", "sk-ant-old")
    credentials.add_key("anthropic", "personal", "sk-ant-new")
    entries = credentials.list_keys("anthropic")
    assert entries == [{"label": "personal", "key": "sk-ant-new"}]


def test_resolve_api_key_prefers_env_var_over_prompt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    console = Console(file=io.StringIO())
    assert credentials.resolve_api_key("anthropic", console) == "sk-ant-from-env"


def test_resolve_api_key_without_env_and_prompt_disabled_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    console = Console(file=io.StringIO())
    with pytest.raises(RuntimeError):
        credentials.resolve_api_key("anthropic", console, allow_prompt=False)


def test_resolve_api_key_without_env_non_interactive_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    console = Console(file=io.StringIO())
    with pytest.raises(RuntimeError):
        credentials.resolve_api_key("anthropic", console, allow_prompt=True)
