"""저장된 ReAct 트레이스를 사람이 읽기 좋게 다시 보여준다 — 실행 중이 아니어도,
다른 사람이 공유한 run_id 로도 볼 수 있다. 저장된 데이터(steps/llm_calls/
findings/manifest)만 읽는다 — 아무것도 새로 실행하지 않는다.

    python -m finredteam.replay latest              # 가장 최근 실행
    python -m finredteam.replay 20260825-143952      # run_id 로
    python -m finredteam.replay runs/20260825-143952 # 경로로도 가능
    python -m finredteam.replay latest --full         # 잘림 없이 전체
    python -m finredteam.replay latest --step 5       # 스텝 하나만 깊게 (LLM 원문 포함)

    설치 후에는 `finredteam-replay latest` 로도 실행 가능 (pyproject.toml 참고).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from finredteam import cli_render
from finredteam.storage.db import DB

console = Console()

_RUNS_ROOT = Path("runs")
_DEFAULT_MAX_LEN = cli_render.DEFAULT_MAX_LEN

# 다른 곳(테스트 등)에서 참조하는 경우를 위해 이름을 유지한다.
_verdict_style = cli_render.verdict_style


def _resolve_run_dir(spec: str) -> Path:
    if spec == "latest":
        candidates = sorted(_RUNS_ROOT.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise SystemExit(f"{_RUNS_ROOT}/ 안에 run 이 하나도 없음")
        return candidates[-1].parent

    direct = Path(spec)
    if direct.is_dir() and (direct / "manifest.json").exists():
        return direct

    guess = _RUNS_ROOT / spec
    if guess.is_dir() and (guess / "manifest.json").exists():
        return guess

    raise SystemExit(f"run 을 찾을 수 없음: {spec!r} (시도: {direct}, {guess})")


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _print_llm_calls_for_step(llm_calls: list[dict[str, Any]], step_no: int) -> None:
    matches = [c for c in llm_calls if c.get("step") == step_no]
    if not matches:
        console.print("[dim](이 스텝의 LLM 호출 원문 없음 — --fake-llm 실행이거나 실제 API 를 안 쓴 실행)[/dim]")
        return
    for call in matches:
        kind = call.get("kind")
        if kind == "call_started":
            console.print(Panel(call.get("user_prompt") or "", title="user prompt", border_style="dim"))
        elif kind == "attempt_response":
            title = f"attempt {call.get('attempt')} raw response (parsed_ok={call.get('parsed_ok')})"
            console.print(Panel(call.get("raw_response") or "", title=title, border_style="dim"))
            if call.get("parse_error"):
                console.print(f"[yellow]parse_error:[/yellow] {call['parse_error']}")
        elif kind == "call_finished":
            if call.get("error"):
                console.print(f"[bold red]API 오류:[/bold red] {call['error']}")
            latency = call.get("latency_ms")
            console.print(f"[dim]latency: {latency:.0f}ms[/dim]" if latency is not None else "")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="finredteam.replay")
    parser.add_argument("run", help="run_id, runs/<id> 경로, 또는 'latest'")
    parser.add_argument("--full", action="store_true", help="Observation/프롬프트를 자르지 않고 전부 보여준다")
    parser.add_argument("--step", type=int, default=None, help="이 스텝 하나만, LLM 호출 원문까지 깊게 본다")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    run_dir = _resolve_run_dir(args.run)
    manifest = _load_manifest(run_dir)
    db = DB(run_dir)

    steps = list(db.read_all("steps"))
    llm_calls = list(db.read_all("llm_calls"))
    findings = list(db.read_all("findings"))

    cli_render.print_manifest_header(console, manifest)

    if args.step is not None:
        matching = [s for s in steps if s.get("step") == args.step]
        if not matching:
            console.print(f"[red]스텝 {args.step} 없음 (총 {len(steps)}스텝)[/red]")
            return 1
        s = matching[0]
        cli_render.print_step(
            console, s.get("step"), s.get("action"), s.get("thought"), s.get("action_input") or {},
            s.get("observation"), full=True, max_len=_DEFAULT_MAX_LEN,
        )
        _print_llm_calls_for_step(llm_calls, args.step)
        return 0

    for s in steps:
        cli_render.print_step(
            console, s.get("step"), s.get("action"), s.get("thought"), s.get("action_input") or {},
            s.get("observation"), full=args.full, max_len=_DEFAULT_MAX_LEN,
        )

    cli_render.print_findings_table(console, findings)
    console.print(f"[dim]transcript: {run_dir / 'transcript.md'}[/dim]")
    console.print(f"[dim]full log:   {run_dir / 'agent.log'}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
