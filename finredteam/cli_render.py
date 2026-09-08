"""CLI 출력 공통 렌더링 — 실행 중(`__main__.py`)과 재생(`replay.py`)이
똑같은 모양으로 보이게 한다. 같은 색·같은 레이아웃을 한 곳에서만 정의한다.
"""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

ACTION_COLOR = {
    "observe_graph": "cyan",
    "recall_memory": "cyan",
    "learn_baseline": "blue",
    "discover_surface": "magenta",
    "propose_hypothesis": "yellow",
    "execute_experiment": "green",
    "finish": "bold red",
}

DEFAULT_MAX_LEN = 600


def verdict_style(observation: str) -> str:
    if "VIOLATION" in observation:
        return "bold red"
    if "ANOMALY" in observation:
        return "bold yellow"
    if "PASS" in observation or "이상 없음" in observation:
        return "green"
    if "UNKNOWN" in observation or "오류" in observation or "실패" in observation:
        return "yellow"
    return "white"


def print_run_header(
    console: Console, *, run_id: str, target: str, provider: str, model: str, mode: str
) -> None:
    """실행 시작 시(`__main__.py`) 찍는 헤더 — `print_manifest_header` 와 짝을 이룬다."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Run", run_id)
    table.add_row("Target", target)
    table.add_row("LLM", f"{provider}/{model}")
    table.add_row("Mode", mode)
    console.print(Panel(table, title="FinRedTeam", border_style="bold blue", expand=False))
    console.print()


def print_manifest_header(console: Console, manifest: dict[str, Any], *, title: str = "FinRedTeam Replay") -> None:
    """실행이 끝난 뒤(`replay.py`) manifest 로부터 같은 모양의 헤더를 찍는다."""
    llm = manifest.get("llm") or {}
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Run", str(manifest.get("run_id")))
    table.add_row("Target", str(manifest.get("target")))
    if manifest.get("engagement_id"):
        table.add_row("Engagement", f"{manifest['engagement_id']}  ({manifest.get('policy_hash', '?')})")
    table.add_row("LLM", f"{llm.get('provider')}/{llm.get('model')}")
    table.add_row("Termination", str(manifest.get("termination_reason")))
    table.add_row("Steps", str(manifest.get("round")))
    table.add_row(
        "Tokens",
        f"{manifest.get('tokens_in', 0)} in / {manifest.get('tokens_out', 0)} out "
        f"(${manifest.get('cost_estimate_usd', 0.0):.4f})",
    )
    console.print(Panel(table, title=title, border_style="bold blue", expand=False))
    console.print()


def print_observation(console: Console, observation: str, *, full: bool = False, max_len: int = DEFAULT_MAX_LEN) -> None:
    stripped = observation.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            console.print_json(data=parsed)
            return

    style = verdict_style(observation)
    if full or len(observation) <= max_len:
        text = observation
    else:
        text = observation[:max_len] + f" … (+{len(observation) - max_len}자, --full 로 전체 보기)"
    console.print(f"[{style}]{text}[/{style}]")


def print_step(
    console: Console,
    step_no: int,
    action: str,
    thought: str,
    action_input: dict[str, Any],
    observation: str,
    *,
    full: bool = False,
    max_len: int = DEFAULT_MAX_LEN,
) -> None:
    action = action or "(없음)"
    color = ACTION_COLOR.get(action, "white")
    console.rule(f"[bold {color}]#{step_no}  {action}[/bold {color}]", style=color, align="left")
    console.print(f"[dim]Thought:[/dim]     {thought or '(없음)'}")
    if action_input:
        console.print(f"[dim]Input:[/dim]       {action_input}")
    console.print("[dim]Observation:[/dim]")
    print_observation(console, observation or "", full=full, max_len=max_len)
    console.print()


def print_findings_table(console: Console, findings: list[dict[str, Any]]) -> None:
    violations = [f for f in findings if f.get("verdict") == "VIOLATION"]
    console.rule("[bold]Findings[/bold]")
    if not violations:
        console.print("[dim]VIOLATION 없음[/dim]\n")
        return
    table = Table(show_lines=True)
    table.add_column("Invariant", style="bold red")
    table.add_column("Operation")
    table.add_column("Method Path")
    table.add_column("Reason")
    for f in violations:
        c = f.get("candidate", {})
        table.add_row(
            f.get("invariant") or "?",
            c.get("operation") or "?",
            f"{c.get('method')} {c.get('path')}",
            (f.get("reason") or "")[:200],
        )
    console.print(table)
    console.print()


def print_report_summary(console: Console, report: dict[str, Any]) -> None:
    """`reporting/report.py::build()` 의 결과 — VVR/Precision 없이 자체 진단
    수치만(§9). raw JSON 대신 표로 보여준다."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row(
        "Verified",
        f"{report.get('verified_count')}/{report.get('candidate_count')} "
        f"(yield {report.get('verification_yield')})",
    )
    table.add_row("Anomaly", str(report.get("anomaly_count")))
    table.add_row("Unknown", str(report.get("unknown_count")))
    table.add_row("Pass", str(report.get("pass_count")))
    console.print(Panel(table, title="Report Summary", border_style="bold green", expand=False))

    for key, title in (("by_layer", "by_layer"), ("by_operation", "by_operation"), ("by_invariant", "by_invariant")):
        section = report.get(key) or {}
        if not section:
            continue
        sub = Table(title=title, show_header=True)
        sub.add_column("key")
        sub.add_column("counts")
        for name, value in section.items():
            counts = ", ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value)
            sub.add_row(str(name), counts)
        console.print(sub)
    console.print()

    objectives = report.get("objectives") or []
    if objectives:
        obj_table = Table(title="달성한 공격 목표 (Objectives — red team view)", show_header=True)
        obj_table.add_column("objective", style="bold")
        obj_table.add_column("상태")
        obj_table.add_column("chain")
        obj_table.add_column("근거")
        for o in objectives:
            achieved = o.get("verdict") == "VIOLATION"
            status = "[bold red]ACHIEVED[/]" if achieved else "[yellow]FOOTHOLD[/]"
            obj_table.add_row(
                o.get("objective", "?"), status,
                "+".join(o.get("chain") or []) or "-",
                (o.get("reason") or "")[:70],
            )
        console.print(obj_table)
        console.print()
