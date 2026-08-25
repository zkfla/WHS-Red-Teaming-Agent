"""`setup_cli.py::_resolve_provider` — provider 자동 감지(env var + codex 로그인
파일) 및 애매할 때의 대화형/비대화형 폴백을 검증한다."""
from __future__ import annotations

import argparse
import json

import pytest
from rich.console import Console

from finredteam import setup_cli


class _FakeStdin:
    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _args(llm_provider=None, no_key_prompt=False) -> argparse.Namespace:
    return argparse.Namespace(llm_provider=llm_provider, no_key_prompt=no_key_prompt)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # 로컬 개발 머신에 진짜 codex 로그인 파일이 있어도 테스트는 영향받지 않게 존재하지 않는
    # 경로로 고정한다(codex 감지를 직접 테스트하는 케이스는 각자 다시 monkeypatch 한다).
    from finredteam.llm import codex_auth

    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", tmp_path / "does-not-exist-auth.json")


def test_explicit_flag_wins_over_everything(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert setup_cli._resolve_provider(_args(llm_provider="chatgpt_subscription"), Console()) == "chatgpt_subscription"


def test_only_anthropic_env_detected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert setup_cli._resolve_provider(_args(), Console()) == "anthropic"


def test_only_openai_env_detected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert setup_cli._resolve_provider(_args(), Console()) == "openai"


def test_only_codex_login_detected(monkeypatch, tmp_path):
    from finredteam.llm import codex_auth

    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", path)
    assert setup_cli._resolve_provider(_args(), Console()) == "chatgpt_subscription"


def test_none_detected_non_interactive_defaults_anthropic(monkeypatch):
    monkeypatch.setattr(setup_cli.sys, "stdin", _FakeStdin(is_tty=False))
    assert setup_cli._resolve_provider(_args(), Console()) == "anthropic"


def test_none_detected_no_key_prompt_flag_defaults_anthropic(monkeypatch):
    monkeypatch.setattr(setup_cli.sys, "stdin", _FakeStdin(is_tty=True))
    assert setup_cli._resolve_provider(_args(no_key_prompt=True), Console()) == "anthropic"


def test_ambiguous_multiple_detected_prompts_and_respects_choice(monkeypatch, tmp_path):
    from finredteam.llm import codex_auth

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", path)
    monkeypatch.setattr(setup_cli.sys, "stdin", _FakeStdin(is_tty=True))
    monkeypatch.setattr(setup_cli.Prompt, "ask", lambda *a, **k: "3")
    assert setup_cli._resolve_provider(_args(), Console()) == "chatgpt_subscription"


def test_ambiguous_none_detected_prompts_and_respects_choice(monkeypatch):
    monkeypatch.setattr(setup_cli.sys, "stdin", _FakeStdin(is_tty=True))
    monkeypatch.setattr(setup_cli.Prompt, "ask", lambda *a, **k: "2")
    assert setup_cli._resolve_provider(_args(), Console()) == "openai"


def test_eof_during_prompt_defaults_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(setup_cli.sys, "stdin", _FakeStdin(is_tty=True))

    def _raise(*a, **k):
        raise EOFError

    monkeypatch.setattr(setup_cli.Prompt, "ask", _raise)
    assert setup_cli._resolve_provider(_args(), Console()) == "anthropic"


def test_codex_login_available_true_for_chatgpt_mode(tmp_path, monkeypatch):
    from finredteam.llm import codex_auth

    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", path)
    assert setup_cli._codex_login_available() is True


def test_codex_login_available_false_for_other_mode(tmp_path, monkeypatch):
    from finredteam.llm import codex_auth

    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "api_key"}), encoding="utf-8")
    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", path)
    assert setup_cli._codex_login_available() is False


def test_codex_login_available_false_when_missing(tmp_path, monkeypatch):
    from finredteam.llm import codex_auth

    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", tmp_path / "does-not-exist.json")
    assert setup_cli._codex_login_available() is False


def test_codex_login_available_false_when_invalid_json(tmp_path, monkeypatch):
    from finredteam.llm import codex_auth

    path = tmp_path / "auth.json"
    path.write_text("{not valid", encoding="utf-8")
    monkeypatch.setattr(codex_auth, "DEFAULT_AUTH_PATH", path)
    assert setup_cli._codex_login_available() is False
