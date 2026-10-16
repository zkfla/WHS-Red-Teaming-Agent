"""Explicit, recoverable ChatGPT device reauthentication for LiteLLM.

The normal provider path may reuse a cached token that the server has already
rejected.  Reauthentication therefore runs only on an explicit operator action:
the cache is moved aside, LiteLLM performs its device flow in a child process,
and the old cache is restored if the new login does not complete.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class ChatGPTReauthError(RuntimeError):
    """Stable failure without provider response text or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ChatGPTReauthResult:
    backup_created: bool


LoginRunner = Callable[[Mapping[str, str]], Awaitable[int]]


def _auth_file(env: Mapping[str, str]) -> Path:
    token_dir = env.get("CHATGPT_TOKEN_DIR", "~/.config/litellm/chatgpt")
    name = env.get("CHATGPT_AUTH_FILE", "auth.json")
    return Path(os.path.join(os.path.expanduser(token_dir), name))


def _move_aside(path: Path, label: str) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ChatGPTReauthError("chatgpt_auth_cache_unsafe")
    destination = path.with_name(f"{path.name}.{label}.{uuid4().hex[:8]}")
    path.replace(destination)
    destination.chmod(0o600)
    return destination


def _restore(backup: Path | None, auth_file: Path) -> None:
    if auth_file.exists() or auth_file.is_symlink():
        _move_aside(auth_file, "failed")
    if backup is not None and backup.exists():
        backup.replace(auth_file)
        auth_file.chmod(0o600)


async def _run_login_subprocess(env: Mapping[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.llm.chatgpt_reauth",
        "--device-login",
        env=dict(env),
    )
    try:
        return await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise


async def reauthenticate_chatgpt(
    *,
    provider: str | None,
    model: str | None,
    env: Mapping[str, str] | None = None,
    runner: LoginRunner | None = None,
) -> ChatGPTReauthResult:
    """Force a fresh device login and preserve the previous cache on failure."""
    if provider != "litellm" or not (model or "").startswith("chatgpt/"):
        raise ChatGPTReauthError("chatgpt_reauth_not_applicable")

    child_env = dict(os.environ if env is None else env)
    auth_file = _auth_file(child_env)
    auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = _move_aside(auth_file, "backup")
    login = runner or _run_login_subprocess
    try:
        return_code = await login(child_env)
        if return_code != 0 or not auth_file.is_file() or auth_file.is_symlink():
            raise ChatGPTReauthError("chatgpt_reauth_failed")
        auth_file.chmod(0o600)
    except asyncio.CancelledError:
        _restore(backup, auth_file)
        raise
    except ChatGPTReauthError:
        _restore(backup, auth_file)
        raise
    except Exception:  # noqa: BLE001 - replace provider details with a stable code
        _restore(backup, auth_file)
        raise ChatGPTReauthError("chatgpt_reauth_failed") from None
    return ChatGPTReauthResult(backup_created=backup is not None)


def _device_login() -> int:
    """Child-process entry point; LiteLLM prints the URL/code to the terminal."""
    try:
        from litellm.llms.chatgpt.authenticator import Authenticator

        token = Authenticator().get_access_token()
        if not isinstance(token, str) or not token:
            return 1
        auth_file = _auth_file(os.environ)
        if not auth_file.is_file() or auth_file.is_symlink():
            return 1
        auth_file.chmod(0o600)
        return 0
    except Exception:  # noqa: BLE001 - provider details can contain sensitive data
        print("ChatGPT 재로그인에 실패했습니다. 웹 화면에서 상태를 확인하세요.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_device_login() if sys.argv[1:] == ["--device-login"] else 2)
