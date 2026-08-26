"""CLI 진입점.

서브커맨드 없이 그냥 `finredteam` 실행 — 바로 대화형 Engagement Setup 이
시작된다(`engagement/setup.py::run_setup`): 이미 승인된 Engagement 가 있으면
재사용할지 먼저 묻고, 없거나 새로 만들기로 하면 자유 텍스트로 대상/목적을
설명받아 LLM 이 부족한 정보를 한 번에 하나씩 질문해 구체화한다. 최종 승인을
받으면 **그 Engagement 를 그 자리에서 바로 실행한다** — `--target`/
`--engagement` 같은 별도 선택 플래그는 없다(engagement §2). `engagement_id`
는 사용자가 짓지 않고 시스템이 자동 생성한다.

`setup` 서브커맨드: 같은 대화형 흐름을 실행 없이 준비만 해둔다.

`--smoke`: 부트스트랩(주체 등록/로그인) + 매니페스트 생성만 확인하고 종료.
그 외: `agent/react.py` 의 ReAct 루프를 돌린다 — BOOTSTRAP → RECON →
ENTITLEMENT → BASELINE → [Thought → Action → Observation] 반복.
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
from finredteam.config import MAX_ROUNDS, LLMConfig, RunConfig
from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.errors import PolicyViolation
from finredteam.engagement.schema import EngagementCredential
from finredteam.logging_setup import configure_logging
from finredteam.models.core import Principal
from finredteam.probing.session import Session, SessionError
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import load_target, minimal_target_config

console = Console()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="finredteam")
    parser.add_argument("--smoke", action="store_true", help="부트스트랩 + 매니페스트만 확인하고 종료")
    parser.add_argument(
        "--llm-provider",
        choices=["anthropic", "openai", "chatgpt_subscription"],
        default=None,
        help="생략하면 환경변수/~/.codex/auth.json 으로 자동 판단하고, 그래도 애매하면 물어본다.",
    )
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument(
        "--fake-llm",
        action="store_true",
        help="결정적 가짜 LLM 사용 — Engagement Setup 대화와 ReAct 루프 둘 다 즉시/고정된 방식으로 "
        "진행한다 (API 키 불필요, 실제 취약점 탐색 품질 검증용 아님)",
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
    parser.add_argument(
        "--new-engagement",
        action="store_true",
        help="이미 승인된 Engagement 가 있어도 재사용 여부를 묻지 않고 무조건 새로 인터뷰한다 "
        "(isatty() 오판·비대화형 환경 등으로 재사용 여부를 물어볼 기회 자체가 없을 때 "
        "확실하게 새 스코프 인터뷰를 강제하는 용도)",
    )
    return parser.parse_args(argv)


def _bootstrap_principals(
    session: Session,
    principal_ids: list[str],
    run_id: str,
    credentials: list[EngagementCredential] | None = None,
    *,
    allow_account_creation: bool,
) -> list[Principal]:
    """`--smoke` 전용 경로 — `agent/react.py::bootstrap_principals` 와 동일한
    credentials-aware 로직(§ engagement 3): 제공된 계정만큼은 로그인만, 나머지는
    `allow_account_creation` 일 때만 새로 등록, 둘 다 아니면 실패."""
    credentials = credentials or []
    principals: list[Principal] = []
    missing_without_creation: list[str] = []
    for i, pid in enumerate(principal_ids):
        try:
            if i < len(credentials):
                cred = credentials[i]
                principal = session.login(Principal(id=pid, username=cred.username, password=cred.password))
            elif allow_account_creation:
                suffix = secrets.token_hex(3)
                username = f"{pid}_{run_id}_{suffix}"
                password = f"Rt!{secrets.token_urlsafe(8)}"
                principal = session.bootstrap(pid, username, password)
            else:
                missing_without_creation.append(pid)
                continue
        except SessionError as exc:
            console.print(f"[red]bootstrap 실패[/red] {pid}: {exc}")
            raise
        principals.append(principal)
    if missing_without_creation:
        raise PolicyViolation(
            "ACCOUNT_CREATION_FORBIDDEN",
            f"주체 {missing_without_creation} 에 대한 사전 제공 계정이 없고 "
            "Engagement 정책이 계정 생성을 허용하지 않음 (--smoke 도 예외 없음)",
        )
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


def _run_smoke(target_cfg, session: Session, store: RunStore, policy, credentials: list[EngagementCredential]) -> None:
    currency = target_cfg.adapter.default_currency
    principals = _bootstrap_principals(
        session,
        target_cfg.settings.principals,
        store.run_id,
        credentials,
        allow_account_creation=policy.allow_account_creation,
    )

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
    table.add_row("Bootstrap", "  ".join(p.id for p in principals))
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


def _run_full(
    target_cfg,
    session: Session,
    store: RunStore,
    llm,
    max_rounds: int | None,
    policy,
    credentials: list[EngagementCredential],
) -> None:
    from finredteam.agent import loop as agent_loop
    from finredteam.models.core import Verdict
    from finredteam.reporting import report as report_module

    console.rule("[bold]Trace[/bold]", style="blue")
    console.print()
    ctx = agent_loop.run(
        target_cfg,
        session,
        llm,
        store,
        policy,
        credentials=credentials,
        max_rounds=max_rounds,
        on_step=_make_on_step(console),
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

    # ★ 이건 판정이 아니다 — 오라클이 이미 확정한 결과를 사람이 읽기 쉽게
    # 풀어서 설명만 한다(§ reporting/summarize_run.py). 실패해도 실행 결과
    # 자체는 이미 저장 완료된 뒤라 조용히 건너뛴다.
    from finredteam.reporting import summarize_run

    narrative = summarize_run.summarize(llm, rep, [f.model_dump(mode="json") for f in ctx.findings])
    if narrative:
        console.print()
        console.print(Panel(narrative, title="AI 요약 (참고용 — 판정 근거 아님)", border_style="magenta"))
        (store.run_dir / "ai_summary.md").write_text(narrative, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")  # logging 콘솔 핸들러가 기본으로 stderr 를 씀
    except (AttributeError, ValueError):
        pass

    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "setup":
        from finredteam.setup_cli import run_setup_command

        return run_setup_command(argv[1:], console)

    args = _parse_args(argv)

    # LLM 클라이언트는 지연 생성한다(build_llm_factory) — 이미 승인된 Engagement 를
    # 재사용하는 경로에서는 API 키를 물어볼 필요가 아예 없어야 하기 때문이다.
    from finredteam.engagement.setup import run_setup
    from finredteam.setup_cli import build_llm_factory

    llm_factory = build_llm_factory(args, console)

    try:
        engagement = run_setup(console, llm_factory, allow_reuse=not args.new_engagement)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    if engagement is None:
        return 1

    policy = compile_policy(engagement)
    target_name = engagement.target.name

    try:
        target_cfg = load_target(target_name)
    except FileNotFoundError:
        # ★ Human pre-analysis 제거 — 어댑터 YAML이 없어도 실행을 막지 않는다.
        # base_url만으로 최소 구성을 만들고, 로그인/회원가입/송금 등은 Black-box
        # Auto-Discovery(recon/auth_discovery.py 등)가 실행 중에 직접 찾는다.
        # 그 발견마저 실패하는 부분만 `probing/session.py`가 명확한 에러로
        # 알린다 — 조용히 성공한 것처럼 보이지 않는다(§11).
        console.print(
            Panel(
                f"[yellow]targets/configs/{target_name}.yaml 없음 — 어댑터 설정 없이 "
                "Black-box Auto-Discovery만으로 진행합니다(로그인/회원가입/송금 등을 "
                "실행 중에 직접 찾습니다). 발견에 실패하는 부분이 있으면 그 지점에서 "
                "명확한 에러로 멈춥니다(조용히 넘어가지 않음).[/yellow]",
                border_style="yellow",
            )
        )
        target_cfg = minimal_target_config(
            target_name, engagement.target.base_url, principal_count=max(3, len(engagement.target.credentials))
        )

    # 아직 provider 를 실제로 정하기 전(그건 llm_factory() 안에서 지연 판단된다)
    # manifest 기록용 임시값 — 실제 실행 후 아래 store.set_llm_display() 로 정정한다.
    from finredteam.setup_cli import DEFAULT_MODEL

    _guessed_provider = args.llm_provider or "anthropic"
    llm_cfg = LLMConfig(provider=_guessed_provider, model=args.llm_model or DEFAULT_MODEL[_guessed_provider])

    run_cfg = RunConfig(
        target_name=target_name,
        target_config_path=Path("finredteam/targets/configs") / f"{target_name}.yaml",
        llm=llm_cfg,
        smoke=args.smoke,
        max_rounds=args.max_rounds or MAX_ROUNDS,
    )
    store = RunStore.start(run_cfg, engagement_id=engagement.engagement_id, policy_hash=policy.policy_hash)

    # 콘솔(레벨 조절 가능) + 파일(runs/<run_id>/agent.log, 항상 DEBUG) 로깅 —
    # LLMError 경고·처리되지 않은 예외·raw 응답 등이 여기 남는다(§ DEBUGGING.md).
    configure_logging(store.run_dir, args.log_level)

    llm = None
    if not args.smoke:
        try:
            # Engagement Setup 대화에서 이미 만든 클라이언트가 있으면 그대로 재사용한다
            # (캐시됨, `build_llm_factory`) — API 키를 두 번 묻지 않는다.
            llm = llm_factory()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            console.print("API 키 없이 배관만 시험하려면 --fake-llm 을 쓰세요.")
            return 1
        if not args.fake_llm:
            llm.set_trace_hook(_make_trace_hook(store, console))

    display_provider = llm.provider if llm is not None else llm_cfg.provider
    display_model = llm.model if llm is not None else llm_cfg.model
    if llm is not None:
        # provider 는 llm_factory() 안에서 지연 판단됐다(플래그/환경변수/대화형 질문) —
        # 실제로 뭘 썼는지로 manifest 를 정정한다. fake-llm 뿐 아니라 항상 갱신한다.
        store.set_llm_display(display_provider, display_model)

    cli_render.print_run_header(
        console,
        run_id=store.run_id,
        target=target_name,
        provider=display_provider,
        model=display_model,
        mode="smoke" if args.smoke else ("fake-llm" if args.fake_llm else "full"),
    )
    console.print(f"[dim]engagement: {engagement.engagement_id}  policy_hash: {policy.policy_hash}[/dim]\n")

    try:
        with Session(target_cfg, policy) as session:
            if args.smoke:
                _run_smoke(target_cfg, session, store, policy, engagement.target.credentials)
            else:
                _run_full(target_cfg, session, store, llm, args.max_rounds, policy, engagement.target.credentials)
    except PolicyViolation as exc:
        console.print(f"[red]정책 위반으로 실행이 거부됨 ({exc.rule}):[/red] {exc.detail}")
        store.finalize(termination_reason=f"policy_violation:{exc.rule}")
        return 1

    console.print(f"[bold]Run saved:[/bold] {store.run_dir}")
    console.print(f"[dim]다시 보기:  python -m finredteam.replay {store.run_id}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
