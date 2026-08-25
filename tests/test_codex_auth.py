"""codex_auth.py — `~/.codex/auth.json` 로더. 실제 파일 스키마는 실측
확인됐다(2026-08-26, 실제 로컬 Codex 로그인으로 라이브 검증) — 여기선 그
스키마를 그대로 흉내낸 fixture 만 쓴다, 진짜 토큰 값은 어디에도 없다."""
from __future__ import annotations

import base64
import json

import pytest

from finredteam.llm.codex_auth import (
    CodexAuthFileNotFound,
    CodexCredentialInvalid,
    UnsupportedCodexAuthMode,
    load_codex_credential,
)


def _write_auth(tmp_path, payload: dict):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fake_id_token(chatgpt_account_id: str | None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": chatgpt_account_id}} if chatgpt_account_id else {}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_missing_file_raises_codex_auth_file_not_found(tmp_path):
    with pytest.raises(CodexAuthFileNotFound):
        load_codex_credential(tmp_path / "does-not-exist.json")


def test_invalid_json_raises_credential_invalid(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CodexCredentialInvalid):
        load_codex_credential(path)


def test_unsupported_auth_mode_raises(tmp_path):
    path = _write_auth(
        tmp_path,
        {"auth_mode": "api_key", "tokens": {"access_token": "x", "account_id": "y"}},
    )
    with pytest.raises(UnsupportedCodexAuthMode):
        load_codex_credential(path)


def test_missing_tokens_field_raises(tmp_path):
    path = _write_auth(tmp_path, {"auth_mode": "chatgpt"})
    with pytest.raises(CodexCredentialInvalid):
        load_codex_credential(path)


def test_missing_access_token_raises(tmp_path):
    path = _write_auth(tmp_path, {"auth_mode": "chatgpt", "tokens": {"account_id": "y"}})
    with pytest.raises(CodexCredentialInvalid):
        load_codex_credential(path)


def test_valid_credential_loads_successfully(tmp_path):
    path = _write_auth(
        tmp_path,
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "at-123",
                "account_id": "acc-123",
                "refresh_token": "rt-123",
            },
        },
    )
    cred = load_codex_credential(path)
    assert cred.access_token == "at-123"
    assert cred.account_id == "acc-123"
    assert cred.refresh_token == "rt-123"


def test_account_id_falls_back_to_id_token_claim_when_missing(tmp_path):
    path = _write_auth(
        tmp_path,
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "at-123",
                "id_token": _fake_id_token("acc-from-jwt"),
            },
        },
    )
    cred = load_codex_credential(path)
    assert cred.account_id == "acc-from-jwt"


def test_account_id_missing_everywhere_raises(tmp_path):
    path = _write_auth(
        tmp_path,
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "at-123", "id_token": _fake_id_token(None)},
        },
    )
    with pytest.raises(CodexCredentialInvalid):
        load_codex_credential(path)


def test_refresh_token_optional(tmp_path):
    path = _write_auth(
        tmp_path,
        {"auth_mode": "chatgpt", "tokens": {"access_token": "at-123", "account_id": "acc-123"}},
    )
    cred = load_codex_credential(path)
    assert cred.refresh_token is None
