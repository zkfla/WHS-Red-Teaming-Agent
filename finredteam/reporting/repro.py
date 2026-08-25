"""VIOLATION Finding(db 레코드) → 재현용 curl 스크립트. §18 조건 11 — 모든
VIOLATION 에 재현 스크립트를 첨부한다.
"""
from __future__ import annotations

import json


def to_curl(record: dict, base_url: str) -> str:
    candidate = record.get("candidate", {})
    lines = [
        f"# {candidate.get('operation')} :: {record.get('invariant')} :: {record.get('verdict')}",
        f"# {record.get('reason', '')}",
    ]
    body_flag = ""
    params = candidate.get("params")
    if params:
        body_flag = f" -H 'Content-Type: application/json' -d '{json.dumps(params, ensure_ascii=False)}'"
    lines.append(
        f"curl -X {candidate.get('method')} '{base_url}{candidate.get('path')}'{body_flag} "
        f"-H 'Authorization: Bearer <{candidate.get('principal_id')}_TOKEN>'"
    )
    return "\n".join(lines)


def build_repro_section(records: list[dict], base_url: str) -> str:
    violations = [r for r in records if r.get("verdict") == "VIOLATION"]
    if not violations:
        return "(재현할 VIOLATION 없음)"
    return "\n\n".join(to_curl(r, base_url) for r in violations)
