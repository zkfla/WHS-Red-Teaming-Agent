"""The explicit ChatGPT login never needs a live provider in tests."""

from __future__ import annotations

import os

import pytest

from redteam.llm.chatgpt_reauth import ChatGPTReauthError, reauthenticate_chatgpt


async def test_reauthentication_moves_old_cache_and_secures_new_cache(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("old-token-record", encoding="utf-8")
    auth_file.chmod(0o644)
    observed_missing = False

    async def login(_env):
        nonlocal observed_missing
        observed_missing = not auth_file.exists()
        auth_file.write_text("new-token-record", encoding="utf-8")
        return 0

    result = await reauthenticate_chatgpt(
        provider="litellm",
        model="chatgpt/gpt-test",
        env={"CHATGPT_TOKEN_DIR": str(tmp_path)},
        runner=login,
    )

    backups = list(tmp_path.glob("auth.json.backup.*"))
    assert observed_missing is True
    assert result.backup_created is True
    assert auth_file.read_text(encoding="utf-8") == "new-token-record"
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old-token-record"
    assert os.stat(auth_file).st_mode & 0o777 == 0o600
    assert os.stat(backups[0]).st_mode & 0o777 == 0o600


async def test_failed_reauthentication_restores_old_cache_and_masks_details(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("old-token-record", encoding="utf-8")

    async def failed_login(_env):
        auth_file.write_text("incomplete-new-record", encoding="utf-8")
        raise RuntimeError("provider-secret-detail")

    with pytest.raises(ChatGPTReauthError) as caught:
        await reauthenticate_chatgpt(
            provider="litellm",
            model="chatgpt/gpt-test",
            env={"CHATGPT_TOKEN_DIR": str(tmp_path)},
            runner=failed_login,
        )

    assert str(caught.value) == "chatgpt_reauth_failed"
    assert "provider-secret-detail" not in str(caught.value)
    assert auth_file.read_text(encoding="utf-8") == "old-token-record"
    failed = list(tmp_path.glob("auth.json.failed.*"))
    assert len(failed) == 1
    assert failed[0].read_text(encoding="utf-8") == "incomplete-new-record"


async def test_reauthentication_rejects_non_chatgpt_configuration_before_runner(tmp_path):
    called = False

    async def login(_env):
        nonlocal called
        called = True
        return 0

    with pytest.raises(ChatGPTReauthError) as caught:
        await reauthenticate_chatgpt(
            provider="litellm",
            model="openai/gpt-test",
            env={"CHATGPT_TOKEN_DIR": str(tmp_path)},
            runner=login,
        )

    assert caught.value.code == "chatgpt_reauth_not_applicable"
    assert called is False


async def test_reauthentication_refuses_symlinked_auth_cache(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("do-not-touch", encoding="utf-8")
    (tmp_path / "auth.json").symlink_to(outside)

    with pytest.raises(ChatGPTReauthError) as caught:
        await reauthenticate_chatgpt(
            provider="litellm",
            model="chatgpt/gpt-test",
            env={"CHATGPT_TOKEN_DIR": str(tmp_path)},
        )

    assert caught.value.code == "chatgpt_auth_cache_unsafe"
    assert outside.read_text(encoding="utf-8") == "do-not-touch"
