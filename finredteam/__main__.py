"""CLI 진입점.

`--smoke`: 부트스트랩(주체 등록/로그인) + 매니페스트 생성만 확인하고 종료 (Day1 범위).
그 외: `agent/loop.py` 의 전체 순환을 돌린다 — RECON → ENTITLEMENT → BASELINE →
[전략 선택 → 후보 → 실행 → 검증] 반복 (§13).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from rich.console import Console

from finredteam.config import MAX_ROUNDS, RunConfig
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
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument(
        "--fake-llm",
        action="store_true",
        help="API 키 없이 F1/F2 배관만 시험하는 결정적 가짜 LLM 사용 (실제 취약점 탐색 품질 검증용 아님)",
    )
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


def _run_smoke(target_cfg, session: Session, store: RunStore) -> None:
    currency = target_cfg.adapter.default_currency
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

    summary = "  ".join(f"{p.id}({currency.code} {target_cfg.settings.initial_balance})" for p in principals)
    console.print(f"[bold]\\[bootstrap][/bold]  {summary}")

    obs_cfg = target_cfg.observation_config()
    ok, total = (0, 0)
    if principals and obs_cfg:
        ok, total = _check_observation(session, principals[0], obs_cfg)
    console.print(
        f"[bold]\\[adapter][/bold]    {target_cfg.adapter.name} - "
        f"observation {ok}/{total} ok, currency {currency.code}({currency.minor_unit})"
    )
    store.finalize(termination_reason="smoke_complete")


def _run_full(target_cfg, session: Session, store: RunStore, llm, max_rounds: int | None) -> None:
    from finredteam.agent import loop as agent_loop
    from finredteam.models.core import Verdict
    from finredteam.reporting import report as report_module

    ctx = agent_loop.run(target_cfg, session, llm, store, max_rounds=max_rounds)

    console.print(f"[bold]\\[steps][/bold]     {ctx.round}  termination={store.manifest.get('termination_reason')}")
    console.print("[bold]\\[trace][/bold]")
    for step in ctx.steps:
        console.print(f"  #{step.step_no} Thought: {step.thought}")
        console.print(f"      Action: {step.action}({step.action_input})")
        console.print(f"      Observation: {step.observation}")
    console.print(
        f"[bold]\\[graph][/bold]     edges={ctx.graph.edge_count()} "
        f"entitled={len(ctx.graph.entitled_edges())} observed={len(ctx.graph.observed_edges())}"
    )

    violations = [f for f in ctx.findings if f.verdict == Verdict.VIOLATION]
    console.print(f"[bold]\\[findings][/bold]  VIOLATION={len(violations)}")
    for finding in violations:
        c = finding.candidate
        console.print(f"  [red]VIOLATION[/red] {finding.invariant} {c.attack_class} {c.method} {c.path}")
        console.print(f"    {finding.reason}")

    rep = report_module.build(store.db, store.manifest)
    console.print("[bold]\\[report][/bold]")
    console.print(json.dumps(rep, ensure_ascii=False, indent=2))
    console.print(
        "※ VVR/Precision 은 evaluator/scorer 로 별도 산출: "
        f"python -m finredteam.evaluator.scorer {store.run_dir}"
    )


def main(argv: list[str] | None = None) -> int:
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

    llm = None
    if not args.smoke:
        if args.fake_llm:
            from finredteam.llm.fake import FakeLLMClient

            llm = FakeLLMClient()
        else:
            from finredteam.llm.factory import build_llm_client

            try:
                llm = build_llm_client(llm_cfg)
            except RuntimeError as exc:
                console.print(f"[red]{exc}[/red]")
                console.print("API 키 없이 배관만 시험하려면 --fake-llm 을 쓰세요.")
                return 1

    run_cfg = RunConfig(
        target_name=args.target,
        target_config_path=Path("finredteam/targets/configs") / f"{args.target}.yaml",
        llm=llm_cfg,
        smoke=args.smoke,
        max_rounds=args.max_rounds or MAX_ROUNDS,
    )
    store = RunStore.start(run_cfg)
    display_provider = llm.provider if llm is not None else llm_cfg.provider
    display_model = llm.model if llm is not None else llm_cfg.model
    if args.fake_llm:
        store.set_llm_display(display_provider, display_model)
    console.print(f"[bold]\\[llm][/bold]        {display_provider} / {display_model}")

    with Session(target_cfg) as session:
        if args.smoke:
            _run_smoke(target_cfg, session, store)
        else:
            _run_full(target_cfg, session, store, llm, args.max_rounds)

    console.print(f"[bold]Report:[/bold] {store.run_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
