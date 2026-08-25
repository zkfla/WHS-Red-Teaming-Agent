"""ReAct 시스템/유저 프롬프트 조립.

★ 프레이밍이 "공격 클래스 카탈로그"가 아니라 **그래프 스키마 + 불변식**
중심이다 — LLM 에게 "F1을 써라/F2를 써라"가 아니라 "Principal, Account,
Transaction, Balance, Authorization 관계를 살피고, 그 관계가 지켜야 할
불변식이 있다"는 걸 먼저 준다. `operation`(read_balance/transfer)은
메커니즘 선택일 뿐이고, 어떤 invariant 가 걸렸는지는 실행 후에 오라클이
결정한다 — 프롬프트도 그 순서를 반영한다.
"""
from __future__ import annotations

from finredteam.planning.actions import ACTION_CATALOG
from finredteam.planning.candidates import AttackTemplate

GRAPH_SCHEMA = """\
이 은행 앱의 핵심 관계 그래프:
  Principal --entitled_own/entitled_read--> Account --involved_in--> Transaction --affects--> Balance
  Authorization = Principal 과 Account/Transaction 사이에 entitled_* 관계가 실제로 있는가

관찰(observed_*)과 권한(entitled_*)은 별개다 — "조회가 됐다"는 사실 자체가
권한의 증거가 되지 않는다. entitled_* 는 정상 가입/로그인 흐름에서만 확정된다.
"""

INVARIANTS = """\
이 그래프 위에서 항상 성립해야 하는 성질들 (실행 후 코드가 자동으로 판정한다 —
당신이 어떤 invariant 를 검사할지 고르는 게 아니다):
  INV-1A 가치 보존      — 통화별로 돈이 생기거나 사라지지 않는다
  INV-1B 인가된 가치 흐름 — 실제 방향·크기가 baseline 실측과 일치한다
  INV-1C 동시성 정합    — 동시 요청 성공 건수 × 단위금액 == 실제 변화
  INV-2  권한 경계      — entitled_* 없이 접근이 성공하면 안 된다
  INV-4  멱등성         — 같은 논리 거래 식별자로 여러 번 성공하면 안 된다
  INV-6  금액 정합성    — 통화 최소단위를 넘는 정밀도, 부호 불일치가 없다
"""

SYSTEM_PREAMBLE = (
    "당신은 인가된 보안 연구용 자율 에이전트입니다. "
    "대상은 로컬 Docker 에서만 실행되는 의도적으로 취약한 교육용 뱅킹 앱입니다. "
    "매 스텝 아래 액션 중 정확히 하나만 골라 JSON 객체 하나로 출력하세요. "
    "실행 절차나 정답을 알려주는 게 아니라, 지금까지 관찰한 그래프·상태 변화를 "
    "바탕으로 다음에 무엇을 확인할지 스스로 판단하세요."
)


def _operations_summary(templates: dict[str, AttackTemplate]) -> str:
    """개념 설명 — "이런 관계를 살펴볼 수 있다"는 참고 자료. 오라클을 정하지 않는다."""
    lines = []
    for template in templates.values():
        considerations = "; ".join(template.agent_must_determine)
        lines.append(f"- {template.name}: {template.concept.strip()}")
        if considerations:
            lines.append(f"  살펴볼 것: {considerations}")
    return "\n".join(lines)


def build_react_system(templates: dict[str, AttackTemplate]) -> str:
    return (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"{GRAPH_SCHEMA}\n"
        f"{INVARIANTS}\n"
        f"사용 가능한 액션:\n{ACTION_CATALOG}\n"
        f"살펴볼 수 있는 관계·시나리오 (참고용 — 오라클을 정하는 게 아니다):\n{_operations_summary(templates)}\n\n"
        '반드시 다음 형식의 JSON 객체 하나만 출력하세요: '
        '{"thought": "...", "action": "...", "action_input": {...}}'
    )


def build_react_user(observation_text: str) -> str:
    return f"{observation_text}\n\n다음 행동을 하나 고르세요."
