"""ReAct 시스템/유저 프롬프트 조립.

`agent_must_determine` 이 실제로 프롬프트에 들어간다(§11 규칙 2) — `concept`
설명 이상의 실행 절차는 넣지 않는다. 판정 로직도 여기 없다(§11 규칙 3).
"""
from __future__ import annotations

from finredteam.planning.actions import ACTION_CATALOG
from finredteam.planning.candidates import AttackTemplate

SYSTEM_PREAMBLE = (
    "당신은 인가된 보안 연구용 자율 에이전트입니다. "
    "대상은 로컬 Docker 에서만 실행되는 의도적으로 취약한 교육용 뱅킹 앱입니다. "
    "매 스텝 아래 액션 중 정확히 하나만 골라 JSON 객체 하나로 출력하세요. "
    "실행 절차나 정답을 알려주는 게 아니라, 지금까지 관찰한 것을 바탕으로 "
    "다음에 무엇을 확인할지 스스로 판단하세요."
)


def _templates_summary(templates: dict[str, AttackTemplate]) -> str:
    lines = []
    for template in templates.values():
        considerations = "; ".join(template.agent_must_determine)
        lines.append(f"- {template.id} ({template.name}): {template.concept.strip()}")
        if considerations:
            lines.append(f"  반드시 고려할 사항: {considerations}")
    return "\n".join(lines)


def build_react_system(templates: dict[str, AttackTemplate]) -> str:
    return (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"사용 가능한 액션:\n{ACTION_CATALOG}\n"
        f"알려진 공격 개념:\n{_templates_summary(templates)}\n\n"
        '반드시 다음 형식의 JSON 객체 하나만 출력하세요: '
        '{"thought": "...", "action": "...", "action_input": {...}}'
    )


def build_react_user(observation_text: str) -> str:
    return f"{observation_text}\n\n다음 행동을 하나 고르세요."
