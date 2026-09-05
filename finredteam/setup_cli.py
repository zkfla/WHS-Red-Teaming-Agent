"""`finredteam setup` CLI 어댑터 — LLM 클라이언트 준비(지연)만 하고, 실제
대화형 Engagement Setup 로직은 `engagement/setup.py::run_setup` 에 그대로
맡긴다.

`finredteam`(서브커맨드 없이) 도 내부적으로 같은 `run_setup` 을 쓴다 — 여기는
"만들기만 하고 바로 실행은 안 하는" 별도 진입점이다(미리 준비해두고 싶을 때).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

from rich.console import Console
from rich.prompt import Prompt

from finredteam.config import LLMConfig

_CHATGPT_MODEL_FALLBACK = "gpt-5.6-terra"


def resolve_chatgpt_model() -> str:
    """ChatGPT 구독(codex) 기본 모델을 고른다.

    codex 백엔드의 모델 슬러그는 자주 바뀐다(예: gpt-5.4 는 폐기되고 gpt-5.6-* /
    gpt-5.5 로 교체됨) — 낡은 슬러그를 하드코딩하면 백엔드가 400('model is not
    supported')으로 거부한다. 그래서 `~/.codex/models_cache.json`(codex login 이
    최신화하는 목록)에서 현재 노출(list)되고 API 지원되는 모델을 골라 self-heal 한다.
    캐시가 없거나 형식이 다르면 안전한 폴백으로 돌아간다.
    """
    try:
        cache = json.load(open(os.path.expanduser("~/.codex/models_cache.json")))
        listed = [
            m["slug"]
            for m in cache.get("models", [])
            if m.get("visibility") == "list" and m.get("supported_in_api") and "mini" not in m.get("slug", "")
        ]
        for pref in (_CHATGPT_MODEL_FALLBACK, "gpt-5.5"):  # 선호 모델 우선, 없으면 첫 노출 모델
            if pref in listed:
                return pref
        return listed[0] if listed else _CHATGPT_MODEL_FALLBACK
    except Exception:
        return _CHATGPT_MODEL_FALLBACK


# provider 별 기본 모델. `--llm-model` 이 없을 때만 쓴다.
DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5",
    "chatgpt_subscription": resolve_chatgpt_model(),  # 캐시 기반 self-heal (슬러그가 자주 바뀜)
}


def _parse_setup_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="finredteam setup")
    parser.add_argument(
        "--llm-provider",
        choices=["anthropic", "openai", "chatgpt_subscription"],
        default=None,
        help="생략하면 환경변수/~/.codex/auth.json 으로 자동 판단하고, 그래도 애매하면"
        "(둘 이상 감지/전부 없음) 물어본다.",
    )
    parser.add_argument("--llm-model", default=None, help=f"기본값은 provider 별로 다름: {DEFAULT_MODEL}")
    parser.add_argument(
        "--fake-llm", action="store_true", help="결정적 가짜 LLM으로 (테스트/CI용 — 질문 없이 즉시 진행)"
    )
    parser.add_argument(
        "--no-key-prompt",
        action="store_true",
        help="API 키 환경변수가 없으면 대화형으로 안 묻고 바로 실패",
    )
    parser.add_argument(
        "--new-engagement",
        action="store_true",
        help="이미 승인된 Engagement 가 있어도 재사용 여부를 묻지 않고 무조건 새로 인터뷰한다",
    )
    return parser.parse_args(argv)


def _codex_login_available() -> bool:
    """`~/.codex/auth.json` 이 존재하고 chatgpt 로그인 모드인지만 가볍게 확인한다
    (토큰 값/서명 검증은 안 함 — 실제 유효성은 어차피 클라이언트 생성 시점에
    `codex_auth.load_codex_credential` 이 제대로 검증한다, 여긴 자동 감지용)."""
    from finredteam.llm.codex_auth import DEFAULT_AUTH_PATH

    try:
        import json

        data = json.loads(DEFAULT_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("auth_mode") == "chatgpt"


def _resolve_provider(args: argparse.Namespace, console: Console) -> str:
    """명시적 플래그 > (대화형이면) 항상 직접 질문(감지된 걸 기본값으로) >
    (비대화형이면) 환경변수/로컬 credential 자동 판단 > 기본값 anthropic.

    감지 대상 셋: `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`(anthropic),
    `OPENAI_API_KEY`(openai), `~/.codex/auth.json`(chatgpt_subscription, `codex
    login` 결과). **대화형 터미널이면 몇 개가 감지되든 항상 1/2/3 중 고를 수
    있게 물어본다** — 자동 감지는 그중 하나를 기본값(Enter 만 치면 선택됨)으로
    미리 골라주는 용도일 뿐, 선택 자체를 대신하지 않는다(여러 provider를 오가며
    비교해보고 싶을 때를 위해). 비대화형(CI/스크립트, `--no-key-prompt`, 파이프
    입력)이면 물어볼 수 없으니 감지된 게 정확히 하나면 그걸, 아니면 기존처럼
    anthropic 기본값을 쓴다."""
    if args.llm_provider:
        return args.llm_provider

    from finredteam.credentials import AUTH_TOKEN_ENV_VAR, ENV_VAR

    anthropic_available = bool(
        os.environ.get(ENV_VAR["anthropic"]) or os.environ.get(AUTH_TOKEN_ENV_VAR["anthropic"])
    )
    openai_available = bool(os.environ.get(ENV_VAR["openai"]))
    codex_available = _codex_login_available()

    detected = [
        p
        for p, ok in (
            ("anthropic", anthropic_available),
            ("openai", openai_available),
            ("chatgpt_subscription", codex_available),
        )
        if ok
    ]

    if args.no_key_prompt or not sys.stdin.isatty():
        return detected[0] if len(detected) == 1 else "anthropic"

    labels = {
        "1": ("anthropic", "Anthropic (Claude)" + (" [감지됨]" if anthropic_available else "")),
        "2": ("openai", "OpenAI (GPT)" + (" [감지됨]" if openai_available else "")),
        "3": ("chatgpt_subscription", "ChatGPT Subscription (codex login)" + (" [감지됨]" if codex_available else "")),
    }
    default_choice = next((k for k, (p, _) in labels.items() if p == detected[0]), "1") if len(detected) == 1 else "1"
    prompt_text = "어떤 LLM 을 쓸까요? " + "  ".join(f"{k}) {v[1]}" for k, v in labels.items())
    try:
        choice = Prompt.ask(prompt_text, choices=list(labels), default=default_choice, console=console)
    except EOFError:
        return detected[0] if len(detected) == 1 else "anthropic"
    return labels[choice][0]


def build_llm_factory(args: argparse.Namespace, console: Console) -> Callable[[], object]:
    """실제로 대화가 시작될 때만(재사용 시엔 전혀) 호출되는 지연 팩토리 —
    그래야 이미 승인된 Engagement 를 재사용하는 경로에서 provider/API 키를
    묻지 않는다. provider 선택도 credential 확보처럼 이 안에서 지연 처리한다."""
    cache: dict[str, object] = {}

    def _get():
        if "llm" in cache:
            return cache["llm"]
        if args.fake_llm:
            from finredteam.llm.fake import FakeLLMClient

            cache["llm"] = FakeLLMClient()
            return cache["llm"]

        provider = _resolve_provider(args, console)
        model = args.llm_model or DEFAULT_MODEL[provider]
        llm_cfg = LLMConfig(provider=provider, model=model)

        from finredteam.llm.factory import build_llm_client

        if provider == "chatgpt_subscription":
            # API 키 개념이 없다 — ChatGPTSubscriptionClient 생성자가 codex login
            # 이 만든 ~/.codex/auth.json 을 직접 읽는다. 여기서 잘못되면(파일 없음/
            # 지원 안 하는 auth_mode/필드 누락) 기존 자리(RuntimeError)로 정규화해서
            # __main__.py/run_setup_command 가 이미 하는 처리를 그대로 탄다.
            from finredteam.llm.codex_auth import (
                CodexAuthFileNotFound,
                CodexCredentialInvalid,
                UnsupportedCodexAuthMode,
            )

            try:
                cache["llm"] = build_llm_client(llm_cfg)
            except (CodexAuthFileNotFound, UnsupportedCodexAuthMode, CodexCredentialInvalid) as exc:
                raise RuntimeError(f"{exc} (codex login 을 실행했는지 확인하세요)") from exc
            return cache["llm"]

        from finredteam.credentials import resolve_credential

        cred = resolve_credential(provider, console, allow_prompt=not args.no_key_prompt)
        if cred.kind == "auth_token":
            cache["llm"] = build_llm_client(llm_cfg, auth_token=cred.value)
        else:
            cache["llm"] = build_llm_client(llm_cfg, api_key=cred.value)
        return cache["llm"]

    return _get


def run_setup_command(argv: list[str], console: Console) -> int:
    from finredteam.engagement.setup import run_setup

    args = _parse_setup_args(argv)
    llm_factory = build_llm_factory(args, console)

    try:
        engagement = run_setup(console, llm_factory, allow_reuse=not args.new_engagement)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if engagement is None:
        return 1

    console.print("[dim]실행하려면: finredteam  (실행 시 이 Engagement 를 재사용할지 물어본다)[/dim]")
    return 0
