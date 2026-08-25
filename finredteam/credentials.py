"""실행 시 LLM 인증 정보를 대화형으로 고르거나 새로 입력할 수 있게 한다.

Anthropic 은 두 가지 인증 방식을 받는다:
- **API 키** (`sk-ant-...`, `ANTHROPIC_API_KEY`) — `x-api-key` 헤더.
- **OAuth 토큰** (`claude setup-token` 으로 생성, `ANTHROPIC_AUTH_TOKEN`) —
  `Authorization: Bearer <token>` 헤더. Claude Code 로그인을 이미 해둔
  사람이 API 키를 따로 발급받지 않고 그대로 쓸 수 있게 해준다.
  (`anthropic` SDK 의 `Anthropic(api_key=..., auth_token=...)` 가 각각을
  그 헤더로 매핑한다 — 뭘 매핑하는지는 여기서 신경 안 써도 된다, `llm/anthropic_.py`
  가 그대로 SDK 에 전달할 뿐이다.)
OpenAI 는 API 키 방식만 있다.

우선순위: 환경변수(ANTHROPIC_API_KEY > ANTHROPIC_AUTH_TOKEN, 또는 OPENAI_API_KEY)
> 로컬에 저장해 둔 인증정보 선택 > 새로 입력. 환경변수가 있으면 아무것도
묻지 않는다 — 자동화(CI, --fake-llm 등)를 방해하지 않기 위해서다.

저장 파일(`.finredteam/credentials.json`)은 프로젝트 루트에 생기고
`.gitignore` 로 커밋에서 제외된다. 평문 저장이므로 개인 PC 밖에서 쓸 정보를
넣지 않는다 — 저장 여부는 매번 사용자에게 물어본다(기본값 아니오).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

_CRED_DIR = Path(".finredteam")
_CRED_PATH = _CRED_DIR / "credentials.json"

ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# claude setup-token 으로 만든 OAuth 토큰 — Anthropic 만 지원한다.
AUTH_TOKEN_ENV_VAR = {
    "anthropic": "ANTHROPIC_AUTH_TOKEN",
}

_KIND_LABEL = {"api_key": "API 키", "auth_token": "OAuth 토큰"}


@dataclass
class Credential:
    kind: str  # "api_key" | "auth_token"
    value: str


def mask(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def _load() -> dict[str, list[dict[str, str]]]:
    if not _CRED_PATH.exists():
        return {}
    try:
        return json.loads(_CRED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, list[dict[str, str]]]) -> None:
    _CRED_DIR.mkdir(exist_ok=True)
    _CRED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(_CRED_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows 등 chmod 가 의미 없는 환경 — 최선 노력일 뿐


def list_keys(provider: str) -> list[dict[str, str]]:
    """저장된 항목 그대로 반환한다. 예전에 `kind` 없이 저장된 항목은
    호출부가 `entry.get("kind", "api_key")` 로 읽어야 한다."""
    return _load().get(provider, [])


def add_key(provider: str, label: str, key: str, kind: str = "api_key") -> None:
    data = _load()
    entries = data.setdefault(provider, [])
    entries[:] = [e for e in entries if e["label"] != label]
    entries.append({"label": label, "key": key, "kind": kind})
    _save(data)


def resolve_credential(
    provider: str,
    console: Console,
    *,
    allow_prompt: bool = True,
) -> Credential:
    """provider 의 인증정보를 구한다: 환경변수 → 저장된 항목 선택 → 새로 입력.

    비대화형(터미널 아님)이거나 `allow_prompt=False` 인데 환경변수도 없으면
    RuntimeError 를 던진다 — 호출자(main)가 `--fake-llm` 안내와 함께 처리한다.
    """
    env_var = ENV_VAR[provider]
    env_val = os.environ.get(env_var)
    if env_val:
        return Credential("api_key", env_val)

    auth_token_var = AUTH_TOKEN_ENV_VAR.get(provider)
    if auth_token_var:
        token_val = os.environ.get(auth_token_var)
        if token_val:
            return Credential("auth_token", token_val)

    env_hint = env_var if not auth_token_var else f"{env_var}/{auth_token_var}"

    if not allow_prompt or not sys.stdin.isatty():
        raise RuntimeError(
            f"{env_hint} 환경변수가 설정되어 있지 않고, 대화형으로 물어볼 수도 없음 "
            f"(--no-key-prompt 이거나 비대화형 실행). {env_hint} 를 설정하거나 --fake-llm 사용."
        )

    saved = list_keys(provider)
    console.print(
        Panel(
            f"{provider} 인증정보가 필요합니다 ({env_hint} 환경변수 없음)",
            border_style="yellow",
            expand=False,
        )
    )

    if saved:
        table = Table(show_header=True)
        table.add_column("#")
        table.add_column("이름")
        table.add_column("종류")
        table.add_column("값")
        for i, entry in enumerate(saved, start=1):
            kind = entry.get("kind", "api_key")
            table.add_row(str(i), entry["label"], _KIND_LABEL.get(kind, kind), mask(entry["key"]))
        table.add_row("n", "[dim](새로 입력)[/dim]", "", "")
        console.print(table)

        choice = Prompt.ask(
            "선택",
            choices=[str(i) for i in range(1, len(saved) + 1)] + ["n"],
            default="1",
        )
        if choice != "n":
            entry = saved[int(choice) - 1]
            return Credential(entry.get("kind", "api_key"), entry["key"])

    kind = "api_key"
    if auth_token_var:
        kind_choice = Prompt.ask(
            "무엇으로 인증할까요? 1) API 키(sk-ant-...)  2) OAuth 토큰(`claude setup-token` 으로 생성)",
            choices=["1", "2"],
            default="1",
        )
        kind = "api_key" if kind_choice == "1" else "auth_token"

    value = Prompt.ask(f"{_KIND_LABEL[kind]} 입력", password=True).strip()
    if not value:
        raise RuntimeError(f"빈 {_KIND_LABEL[kind]}는 사용할 수 없음")

    label = Prompt.ask("이 인증정보에 붙일 이름", default=provider)
    if Confirm.ask(f"다음에 다시 선택할 수 있게 로컬에 저장할까요? ({_CRED_PATH})", default=True):
        add_key(provider, label, value, kind=kind)
        console.print(f"[dim]저장됨: {_CRED_PATH} (git에 커밋되지 않음)[/dim]")

    return Credential(kind, value)
