"""VIOLATION Finding(db 레코드) → 버그바운티 스타일 서술. 서술은 코드가 만든
수치·필드값만 조합한다 — LLM 생성 문장을 넣지 않는다(§18 조건 12).
"""
from __future__ import annotations

from finredteam.reporting.repro import to_curl


def to_writeup(record: dict, base_url: str) -> str:
    candidate = record.get("candidate", {})
    return (
        f"## [{candidate.get('operation')}] {record.get('invariant')} 위반 — "
        f"{candidate.get('method')} {candidate.get('path')}\n\n"
        f"- 주체: {candidate.get('principal_id')}\n"
        f"- 파라미터: {candidate.get('params')}\n"
        f"- 판정 근거: {record.get('reason')}\n"
        f"- 증거 참조: {record.get('evidence_refs')}\n\n"
        f"### 재현\n```bash\n{to_curl(record, base_url)}\n```\n"
    )


def build_report(records: list[dict], base_url: str) -> str:
    violations = [r for r in records if r.get("verdict") == "VIOLATION"]
    if not violations:
        return "# Verified Findings\n\n(없음)\n"
    sections = [to_writeup(r, base_url) for r in violations]
    return "# Verified Findings\n\n" + "\n\n".join(sections)
