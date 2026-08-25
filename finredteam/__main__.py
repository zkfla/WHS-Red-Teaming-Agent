"""CLI 진입점.

`--smoke`: 부트스트랩(주체 등록/로그인) + 매니페스트 생성만 확인하고 종료 (Day1 범위).
그 외: `agent/react.py` 의 ReAct 루프를 돌린다 — BOOTSTRAP → RECON → ENTITLEMENT
→ BASELINE → [Thought → Action → Observation] 반복.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from finredteam import cli_render
from finredteam.config import MAX_ROUNDS, RunConfig
from finredteam.logging_setup import configure_logging
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
        help="API 키 없이 read_balance/transfer 배관만 시험하는 결정적 가짜 LLM 사용 (실제 취약점 탐색 품질 검증용 아님)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="콘솔 로그 레벨 (§ DEBUGGING.md) — LLM 호출 실패·처리되지 않은 예외 등",
    )
    parser.add_argument(
        "--no-key-prompt",
        action="store_true",
        help="API 키 환경변수가 없어도 대화형으로 묻지 않고 바로 실패 (CI 등 비대화형 실행용)",
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

    obs_cfg = target_cfg.observation_config()
    ok, total = (0, 0)
    if principals and obs_cfg:
        ok, total = _check_observation(session, principals[0], obs_cfg)
    store.finalize(termination_reason="smoke_complete")

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Bootstrap", "  ".join(f"{p.id}({currency.code} {target_cfg.settings.initial_balance})" for p in principals))
    table.add_row("Adapter", target_cfg.adapter.name)
    table.add_row("Observation", f"{ok}/{total} ok, currency {currency.code}({currency.minor_unit})")
    console.print(Panel(table, title="Smoke Test OK", border_style="bold green", expand=False))


def _make_on_step(console: Console) -> Callable:
    """스텝이 끝나는 즉시 콘솔에 찍는다 — 루프가 다 끝나야 보이던 걸 실시간으로
    바꾼다(§ DEBUGGING.md 3.1). `agent/react.py` 는 이 콜백의 시그니처만 안다."""

    def _on_step(step) -> None:
        cli_render.print_step(
            console, step.step_no, step.action, step.thought, step.action_input or {}, step.observation
        )

    return _on_step


def _make_trace_hook(store: RunStore, console: Console) -> Callable:
    """LLM 호출 시작/각 시도/종료를 콘솔에 찍고 storage 의 `llm_calls` 컬렉션에
    원문(prompt/raw response)까지 남긴다(§ DEBUGGING.md 3.1, 3.3). 사람이
    디버깅할 때만 쓰는 채널이다 — 판정 파이프라인은 이 값을 읽지 않는다.

    각 레코드에 `step` 번호를 붙인다 — `llm/base.py::LLMEvent` 자체는 "스텝"을
    모른다(ReAct 개념이라 llm/ 층에 넣지 않는다), 대신 CALL_STARTED 가 매 스텝
    정확히 한 번 발생하는 것을 이용해 여기서 카운터로 매긴다. `replay.py --step`
    이 이 번호로 LLM 원문을 스텝과 연결한다.
    """
    from finredteam.llm.base import LLMEventKind

    step_counter = {"n": 0}

    def _on_llm_event(event) -> None:
        if event.kind == LLMEventKind.CALL_STARTED:
            step_counter["n"] += 1

        record = event.model_dump(mode="json")
        record["step"] = step_counter["n"]
        store.append("llm_calls", record)

        if event.kind == LLMEventKind.CALL_STARTED:
            console.print(f"    [dim]LLM 호출 중... ({event.provider}/{event.model})[/dim]")
        elif event.kind == LLMEventKind.CALL_FINISHED:
            if event.error:
                console.print(f"    [red]LLM 호출 실패:[/red] {event.error}")
            else:
                latency = f"{event.latency_ms:.0f}ms" if event.latency_ms is not None else "?"
                console.print(f"    [dim]LLM 응답 수신 (시도 {event.attempt}, {latency})[/dim]")

    return _on_llm_event


def _run_full(target_cfg, session: Session, store: RunStore, llm, max_rounds: int | None) -> None:
    from finredteam.agent import loop as agent_loop
    from finredteam.models.core import Verdict
    from finredteam.reporting import report as report_module

    console.rule("[bold]Trace[/bold]", style="blue")
    console.print()
    ctx = agent_loop.run(
        target_cfg, session, llm, store, max_rounds=max_rounds, on_step=_make_on_step(console)
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("Steps", f"{ctx.round}  (termination={store.manifest.get('termination_reason')})")
    summary.add_row(
        "Graph",
        f"edges={ctx.graph.edge_count()} entitled={len(ctx.graph.entitled_edges())} "
        f"observed={len(ctx.graph.observed_edges())}",
    )
    violations = [f for f in ctx.findings if f.verdict == Verdict.VIOLATION]
    summary.add_row("Findings", f"VIOLATION={len(violations)}")
    console.print(Panel(summary, title="Run Summary", border_style="bold blue", expand=False))
    console.print()

    cli_render.print_findings_table(console, [f.model_dump(mode="json") for f in ctx.findings])

    rep = report_module.build(store.db, store.manifest)
    cli_render.print_report_summary(console, rep)
    console.print(
        "[dim]※ VVR/Precision 은 evaluator/scorer 로 별도 산출: "
        f"python -m finredteam.evaluator.scorer {store.run_dir}[/dim]"
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

    run_cfg = RunConfig(
        target_name=args.target,
        target_config_path=Path("finredteam/targets/configs") / f"{args.target}.yaml",
        llm=llm_cfg,
        smoke=args.smoke,
        max_rounds=args.max_rounds or MAX_ROUNDS,
    )
    store = RunStore.start(run_cfg)

    # 콘솔(레벨 조절 가능) + 파일(runs/<run_id>/agent.log, 항상 DEBUG) 로깅 —
    # LLMError 경고·처리되지 않은 예외·raw 응답 등이 여기 남는다(§ DEBUGGING.md).
    configure_logging(store.run_dir, args.log_level)

    llm = None
    if not args.smoke:
        if args.fake_llm:
            from finredteam.llm.fake import FakeLLMClient

            llm = FakeLLMClient()
        else:
            from finredteam.credentials import resolve_api_key
            from finredteam.llm.factory import build_llm_client

            try:
                api_key = resolve_api_key(
                    llm_cfg.provider, console, allow_prompt=not args.no_key_prompt
                )
                llm = build_llm_client(
                    llm_cfg, trace_hook=_make_trace_hook(store, console), api_key=api_key
                )
            except RuntimeError as exc:
                console.print(f"[red]{exc}[/red]")
                console.print("API 키 없이 배관만 시험하려면 --fake-llm 을 쓰세요.")
                return 1

    display_provider = llm.provider if llm is not None else llm_cfg.provider
    display_model = llm.model if llm is not None else llm_cfg.model
    if args.fake_llm:
        store.set_llm_display(display_provider, display_model)

    cli_render.print_run_header(
        console,
        run_id=store.run_id,
        target=args.target,
        provider=display_provider,
        model=display_model,
        mode="smoke" if args.smoke else ("fake-llm" if args.fake_llm else "full"),
    )

    with Session(target_cfg) as session:
        if args.smoke:
            _run_smoke(target_cfg, session, store)
        else:
            _run_full(target_cfg, session, store, llm, args.max_rounds)

    console.print(f"[bold]Run saved:[/bold] {store.run_dir}")
    console.print(f"[dim]다시 보기:  python -m finredteam.replay {store.run_id}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
