"""CLI 진입점. Day1 범위: `--smoke` 로 부트스트랩(주체 등록/로그인)과 매니페스트
생성만 확인한다. 전체 순환(agent/loop.py)은 Day6에서 연결한다.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from rich.console import Console

from finredteam.config import RunConfig
from finredteam.jsonpath import get_path
from finredteam.models.core import Principal
from finredteam.probing.session import Session, SessionError
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import load_target

console = Console()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="finredteam")
    parser.add_argument("--target", required=True, help="targets/configs/<name>.yaml")
    parser.add_argument("--smoke", action="store_true", help="부트스트랩 + 매니페스트만 확인하고 종료")
    parser.add_argument("--llm-provider", choices=["anthropic", "openai"], default=None)
    parser.add_argument("--llm-model", default=None)
    return parser.parse_args(argv)


def _bootstrap_principals(session: Session, principal_ids: list[str], run_id: str) -> list[Principal]:
    principals: list[Principal] = []
    for pid in principal_ids:
        suffix = secrets.token_hex(3)
        username = f"{pid}_{run_id}_{suffix}"
        password = f"Rt!{secrets.token_urlsafe(8)}"
        try:
            principal = session.bootstrap(pid, username, password)
        except SessionError as exc:
            console.print(f"[red]bootstrap 실패[/red] {pid}: {exc}")
            raise
        principals.append(principal)
    return principals


def _check_observation(session: Session, principal: Principal, obs_cfg: dict) -> tuple[int, int]:
    ok = 0
    total = 0
    for _key, ep in obs_cfg.items():
        if not isinstance(ep, dict) or "path" not in ep:
            continue
        total += 1
        path = ep["path"]
        if "{account_number}" in path:
            if not principal.account_number:
                continue
            path = path.replace("{account_number}", principal.account_number)
        if "{card_id}" in path:
            # Day1 스모크 시점엔 카드가 없다 — 존재 여부만 확인하지 않고 스킵
            continue
        method = ep.get("method", "GET")
        try:
            if ep.get("auth_required", True):
                ex = session.authed_request(method, path, principal)
            else:
                ex = session.anonymous_request(method, path)
        except SessionError:
            continue
        if ex.status_code < 400:
            ok += 1
    return ok, total


def main(argv: list[str] | None = None) -> int:
    # Windows 콘솔 기본 코드페이지(cp949 등)로는 일부 유니코드 출력이 깨진다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = _parse_args(argv if argv is not None else sys.argv[1:])

    target_cfg = load_target(args.target)
    llm_cfg = target_cfg.settings.llm
    if args.llm_provider:
        llm_cfg = llm_cfg.model_copy(update={"provider": args.llm_provider})
    if args.llm_model:
        llm_cfg = llm_cfg.model_copy(update={"model": args.llm_model})

    run_cfg = RunConfig(
        target_name=args.target,
        target_config_path=Path("finredteam/targets/configs") / f"{args.target}.yaml",
        llm=llm_cfg,
        smoke=args.smoke,
    )
    store = RunStore.start(run_cfg)

    console.print(f"[bold]\\[llm][/bold]        {llm_cfg.provider} / {llm_cfg.model}")

    currency = target_cfg.adapter.default_currency
    with Session(target_cfg) as session:
        principals = _bootstrap_principals(session, target_cfg.settings.principals, store.run_id)

        for p in principals:
            store.append(
                "entitlements",
                {
                    "principal": p.id,
                    "relation": "entitled_own",
                    "obj": f"account:{p.account_number}",
                    "source": "self_created",
                    "evidence_ref": None,
                },
            )

        summary = "  ".join(
            f"{p.id}({currency.code} {target_cfg.settings.initial_balance})" for p in principals
        )
        console.print(f"[bold]\\[bootstrap][/bold]  {summary}")

        obs_cfg = target_cfg.observation_config()
        ok, total = (0, 0)
        if principals and obs_cfg:
            ok, total = _check_observation(session, principals[0], obs_cfg)
        console.print(
            f"[bold]\\[adapter][/bold]    {target_cfg.adapter.name} - "
            f"observation {ok}/{total} ok, currency {currency.code}({currency.minor_unit})"
        )

    store.finalize(termination_reason="smoke_complete" if args.smoke else "day1_stub")
    console.print(f"[bold]Report:[/bold] {store.run_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
