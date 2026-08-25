"""Thought/Action/Observation 전체 트레이스 → 사람이 읽는 마크다운.

`reporting/report.py` 와 같은 이유로 VVR/Precision 계산을 하지 않는다 —
이미 storage 에 있는 `steps`/`findings`/`manifest` 로부터 만드는 순수
서술 포매팅일 뿐이다. `agent/react.py::run()` 이 매 `store.finalize()`
직후(정상 종료·LLM 요청 종료·예외로 인한 중단 전부) 이 문서를
`runs/<run_id>/transcript.md` 로 남긴다 — 실행이 죽어도 사후 분석용
기록이 남는다(§ DEBUGGING.md 3.5).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from finredteam.storage.db import DB


def build_transcript(db: DB, manifest: dict[str, Any]) -> str:
    llm_info = manifest.get("llm") or {}
    lines: list[str] = [
        f"# Run {manifest.get('run_id', '?')} — {manifest.get('target', '?')} "
        f"({llm_info.get('provider', '?')}/{llm_info.get('model', '?')})",
        "",
    ]

    steps = list(db.read_all("steps"))
    if not steps:
        lines.append("(스텝 기록 없음)")
    for step in steps:
        lines.append(f"## Step {step.get('step')}")
        lines.append(f"**Thought**: {step.get('thought') or '(없음)'}")
        lines.append(f"**Action**: {step.get('action') or '(없음)'}({step.get('action_input')})")
        lines.append(f"**Observation**: {step.get('observation')}")
        lines.append("")

    lines.append("## Findings")
    violations = [f for f in db.read_all("findings") if f.get("verdict") == "VIOLATION"]
    if not violations:
        lines.append("(VIOLATION 없음)")
    else:
        for finding in violations:
            candidate = finding.get("candidate", {})
            lines.append(
                f"- **[VIOLATION] {finding.get('invariant')}** "
                f"{candidate.get('operation')} {candidate.get('method')} {candidate.get('path')} "
                f"— {finding.get('reason')}"
            )
    lines.append("")

    lines.append("## 종료")
    lines.append(
        f"- reason: `{manifest.get('termination_reason')}`\n"
        f"- steps: {manifest.get('round')}\n"
        f"- tokens: {manifest.get('tokens_in', 0)} in / {manifest.get('tokens_out', 0)} out\n"
        f"- cost: ${manifest.get('cost_estimate_usd', 0.0):.4f}\n"
        f"- requests: {manifest.get('requests', 0)}"
    )
    return "\n".join(lines)


def write_transcript(db: DB, manifest: dict[str, Any], run_dir: Path) -> Path:
    path = Path(run_dir) / "transcript.md"
    path.write_text(build_transcript(db, manifest), encoding="utf-8")
    return path
