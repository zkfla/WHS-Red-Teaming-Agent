"""실행 시 API 키를 대화형으로 고르거나 새로 입력할 수 있게 한다.

우선순위: 환경변수(ANTHROPIC_API_KEY/OPENAI_API_KEY) > 로컬에 저장해 둔 키 선택
> 새로 입력. 환경변수가 있으면 아무것도 묻지 않는다 — 자동화(CI, --fake-llm 등)를
방해하지 않기 위해서다.

저장 파일(`.finredteam/credentials.json`)은 프로젝트 루트에 생기고
`.gitignore` 로 커밋에서 제외된다. 평문 저장이므로 개인 PC 밖에서 쓸 정보를
넣지 않는다 — 저장 여부는 매번 사용자에게 물어본다(기본값 아니오).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

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


def mask(key: str) -> str:
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


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
    return _load().get(provider, [])


def add_key(provider: str, label: str, key: str) -> None:
    data = _load()
    entries = data.setdefault(provider, [])
    entries[:] = [e for e in entries if e["label"] != label]
    entries.append({"label": label, "key": key})
    _save(data)


def resolve_api_key(
    provider: str,
    console: Console,
    *,
    allow_prompt: bool = True,
) -> str:
    """provider 의 API 키를 구한다: 환경변수 → 저장된 키 선택 → 새로 입력.

    비대화형(터미널 아님)이거나 `allow_prompt=False` 인데 환경변수도 없으면
    RuntimeError 를 던진다 — 호출자(main)가 `--fake-llm` 안내와 함께 처리한다.
    """
    env_var = ENV_VAR[provider]
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val

    if not allow_prompt or not sys.stdin.isatty():
        raise RuntimeError(
            f"{env_var} 환경변수가 설정되어 있지 않고, 대화형으로 물어볼 수도 없음 "
            f"(--no-key-prompt 이거나 비대화형 실행). {env_var} 를 설정하거나 --fake-llm 사용."
        )

    saved = list_keys(provider)
    console.print(
        Panel(
            f"{provider} API 키가 필요합니다 ({env_var} 환경변수 없음)",
            border_style="yellow",
            expand=False,
        )
    )

    if saved:
        table = Table(show_header=True)
        table.add_column("#")
        table.add_column("이름")
        table.add_column("키")
        for i, entry in enumerate(saved, start=1):
            table.add_row(str(i), entry["label"], mask(entry["key"]))
        table.add_row("n", "[dim](새 키 입력)[/dim]", "")
        console.print(table)

        choice = Prompt.ask(
            "선택",
            choices=[str(i) for i in range(1, len(saved) + 1)] + ["n"],
            default="1",
        )
        if choice != "n":
            return saved[int(choice) - 1]["key"]

    key = Prompt.ask(f"{provider} API 키 입력", password=True).strip()
    if not key:
        raise RuntimeError("빈 API 키는 사용할 수 없음")

    label = Prompt.ask("이 키에 붙일 이름", default=provider)
    if Confirm.ask(f"다음에 다시 선택할 수 있게 로컬에 저장할까요? ({_CRED_PATH})", default=True):
        add_key(provider, label, key)
        console.print(f"[dim]저장됨: {_CRED_PATH} (git에 커밋되지 않음)[/dim]")

    return key
