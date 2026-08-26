"""실행이 끝난 뒤, 이미 코드가 확정한 결과(report/findings)를 사람이 빠르게
읽을 수 있는 자연어로 풀어서 설명한다.

★ 이건 판정이 아니다 — `Finding.reason`(§18 조건 12: "LLM 생성 문장 금지")과는
완전히 다른 층이다. 오라클이 이미 VIOLATION/PASS/... 을 확정한 *뒤에*, 그
결과를 요약만 한다. 이 모듈이 반환한 문장은 어떤 Verdict/오라클 판정에도
다시 입력되지 않는다 — 순수 부가 기능이라 실패해도(LLMError, 빈 응답) 실행
자체를 막지 않고 조용히 `None` 을 돌려준다.
"""
from __future__ import annotations

import json

from pydantic import BaseModel

from finredteam.llm.base import LLMClient, LLMError

_SYSTEM_PROMPT = """\
너는 보안 Red Teaming 실행 결과를 사람이 빠르게 이해하도록 요약하는 보조자다.

아래 JSON은 이미 코드(오라클)가 확정한 결과다 — 네가 새로 판정하지 않는다,
있는 그대로 쉽게 풀어서 설명만 한다. 다음을 포함해라:
- 무엇을 테스트했고 확정된 취약점(VIOLATION)이 몇 건, 어떤 종류인지
- 왜 이 시점에 종료됐는지(termination_reason)
- 사람이 다음에 확인하면 좋을 것 1~2가지

3~6문장, 한국어, 과장·추측 없이 주어진 수치·필드값만 근거로 서술해라.
JSON으로만 답하라.
"""


class RunNarrative(BaseModel):
    narrative: str


def _trim_finding(finding: dict) -> dict:
    candidate = finding.get("candidate", {})
    return {
        "verdict": finding.get("verdict"),
        "invariant": finding.get("invariant"),
        "operation": candidate.get("operation"),
        "principal_id": candidate.get("principal_id"),
        "method": candidate.get("method"),
        "path": candidate.get("path"),
        "reason": finding.get("reason"),
    }


def summarize(llm: LLMClient, report: dict, findings: list[dict]) -> str | None:
    """`report`: `reporting/report.py::build()` 출력. `findings`: `Finding.model_dump(mode='json')`
    리스트(원본 그대로, 여기서 필요한 필드만 추려 프롬프트에 넣는다)."""
    user_prompt = "실행 결과(JSON):\n" + json.dumps(
        {**report, "findings": [_trim_finding(f) for f in findings]},
        ensure_ascii=False,
        default=str,
    )
    try:
        results = llm.complete_json(_SYSTEM_PROMPT, user_prompt, RunNarrative, temperature=0.3)
    except LLMError:
        return None
    if not results:
        return None
    return results[0].narrative
