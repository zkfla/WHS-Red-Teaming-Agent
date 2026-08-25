"""후보 뼈대(skeleton) + 메모리 요약 → LLM 프롬프트.

`agent_must_determine` 이 실제로 프롬프트에 들어간다(§11 규칙 2) — `concept`
설명 이상의 실행 절차는 넣지 않는다. 판정 로직도 여기 없다(§11 규칙 3).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from finredteam.planning.candidates import AttackTemplate, CandidateSkeleton


class ParamSuggestion(BaseModel):
    """LLM 이 제안하는 시험 파라미터. 판정 근거(expected_delta)는 여기 없다 — planner.py 가
    baseline 에서 따로 계산한다(§6)."""

    params: dict[str, str] = Field(description="요청 바디에 넣을 필드-값 (문자열로)")
    rationale: str = Field(description="이 파라미터를 고른 이유 — 사람이 읽는 근거, 판정에 안 쓰임")


SYSTEM_PREAMBLE = (
    "당신은 인가된 보안 연구용 자율 에이전트의 계획 보조입니다. "
    "대상은 로컬 Docker 에서만 실행되는 의도적으로 취약한 교육용 뱅킹 앱입니다. "
    "실행 절차나 정답을 알려주는 것이 아니라, 아래 공격 개념에 맞는 구체적인 "
    "시험 파라미터 후보를 제안하세요."
)


def build_prompt(
    template: AttackTemplate, skeleton: CandidateSkeleton, memory_summary: str
) -> tuple[str, str]:
    considerations = "\n".join(f"- {q}" for q in template.agent_must_determine)
    system = (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"공격 클래스: {template.id} ({template.name})\n"
        f"개념: {template.concept.strip()}\n\n"
        f"반드시 고려할 사항:\n{considerations}"
    )
    counterparty_account = skeleton.hint.get("counterparty_account")
    user = (
        f"엔드포인트: {skeleton.method} {skeleton.path}\n"
        f"시험 주체: {skeleton.principal_id}\n"
        f"대상 계좌: {skeleton.subject_account or '(없음)'}\n"
        f"상대 계좌(수신자 등, 있다면): {counterparty_account or '(없음)'}\n\n"
        f"이전 시도 요약 (같은 실수를 반복하지 마세요):\n{memory_summary}\n\n"
        "위 정보를 바탕으로 시험할 파라미터 후보를 1~3개 제안하세요."
    )
    return system, user
