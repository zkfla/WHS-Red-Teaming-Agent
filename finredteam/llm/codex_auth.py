"""Codex CLI(`codex login`)가 만드는 `~/.codex/auth.json` 을 읽는다.

실측 확인된 스키마(2026-08-26, 실제 로컬 파일 기준 — 값은 절대 로그로 남기지
않는다):

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token": "<JWT>",
    "access_token": "<JWT>",
    "refresh_token": "<opaque>",
    "account_id": "<uuid>"
  },
  "last_refresh": "<ISO8601>"
}
```

`auth_mode == "chatgpt"` 만 지원한다 — 다른 모드(API 키 모드 등)는 명확한
오류로 거절한다. 자동 refresh(§16 Phase B)는 이번 구현 범위 밖이다 — 401을
받으면 이 파일을 다시 읽어서 한 번만 재시도하고, 그래도 안 되면
`ReauthenticationRequired` 를 던져 `codex login` 재실행을 안내한다
(`chatgpt_subscription.py` 가 담당).
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"


class CodexAuthFileNotFound(Exception):
    """`auth.json` 자체가 없다 — `codex login` 을 먼저 해야 한다."""


class UnsupportedCodexAuthMode(Exception):
    """`auth_mode` 가 `"chatgpt"` 가 아니다 — 이 구현은 그 모드만 지원한다."""


class CodexCredentialInvalid(Exception):
    """JSON 파싱은 됐지만 필요한 필드(access_token/account_id 등)가 없다."""


@dataclass(frozen=True)
class CodexCredential:
    access_token: str
    account_id: str
    refresh_token: str | None


def _account_id_from_id_token(id_token: str | None) -> str | None:
    """`tokens.account_id` 가 없을 때만 쓰는 fallback — id_token(JWT) 의
    `https://api.openai.com/auth.chatgpt_account_id` 클레임을 읽는다(실측
    확인된 클레임 이름). 서명 검증은 하지 않는다 — 여기선 신뢰된 로컬
    파일의 내용을 파싱할 뿐, 인증 자체는 access_token 이 한다."""
    if not id_token:
        return None
    try:
        payload_b64 = id_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    auth_claim = payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claim, dict):
        return None
    account_id = auth_claim.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) else None


def load_codex_credential(path: Path = DEFAULT_AUTH_PATH) -> CodexCredential:
    if not path.exists():
        raise CodexAuthFileNotFound(f"Codex credential not found: {path} — 먼저 'codex login' 을 실행하세요")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexCredentialInvalid(f"{path} 를 읽거나 파싱할 수 없음") from exc

    auth_mode = raw.get("auth_mode")
    if auth_mode != "chatgpt":
        raise UnsupportedCodexAuthMode(
            f"지원하지 않는 auth_mode: {auth_mode!r} — 이 구현은 'chatgpt' 모드만 지원함"
        )

    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexCredentialInvalid("'tokens' 필드가 없거나 객체가 아님")

    access_token = tokens.get("access_token")
    if not access_token:
        raise CodexCredentialInvalid("tokens.access_token 이 없음")

    account_id = tokens.get("account_id") or _account_id_from_id_token(tokens.get("id_token"))
    if not account_id:
        raise CodexCredentialInvalid("account_id 를 확보할 수 없음 (tokens.account_id, id_token 클레임 둘 다 실패)")

    return CodexCredential(
        access_token=access_token,
        account_id=account_id,
        refresh_token=tokens.get("refresh_token"),
    )
