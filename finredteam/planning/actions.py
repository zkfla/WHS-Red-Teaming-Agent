"""ReAct 액션 어휘 — LLM 이 고를 수 있는 액션은 이 5개뿐이다.

자유 텍스트 경로·페이로드를 LLM 이 직접 쓰게 하지 않는다. `propose_candidate`
의 `target`은 그래프가 실제로 아는 노드 id(예: "account:1234567890")여야 하고,
`invariants`(오라클 목록)는 LLM 이 정하지 않는다 — `attack_class` 로부터
`templates/attacks.yaml` 에서 자동으로 끌어온다(§설계원칙 3, §11 규칙 1).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionName = Literal["observe_graph", "recall_memory", "learn_baseline", "propose_candidate", "finish"]

ACTION_CATALOG = """\
- observe_graph: {} — 그래프 요약(알려진 계좌, entitled_own 관계)을 본다. 부작용 없음.
- recall_memory: {"query": "<검색어>"} — 이전 사실·교훈을 검색한다. 부작용 없음.
- learn_baseline: {"op": "<baseline_ops 의 op 이름, 예: transfer>"} — 정상 파라미터로
  한 번 실행해 실측 기준선을 만든다. F2 류 금액 판정 전에 필요할 수 있다.
- propose_candidate: {"attack_class": "F1|F2", "principal_id": "<시험 주체>",
  "target": "account:<계좌번호>", "params": {...}} — 실제로 요청을 보내고 판정까지
  받는다. attack_class 에 맞는 오라클은 자동으로 붙는다(직접 고르지 않는다).
- finish: {"reason": "<왜 그만두는지>"} — 탐색을 종료한다. 예산 한도가 항상 우선한다.
"""


class ObserveGraphInput(BaseModel):
    pass


class RecallMemoryInput(BaseModel):
    query: str = ""


class LearnBaselineInput(BaseModel):
    op: str


class ProposeCandidateInput(BaseModel):
    attack_class: str
    principal_id: str
    target: str = ""
    params: dict[str, str] = {}


class FinishInput(BaseModel):
    reason: str = ""


_INPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "observe_graph": ObserveGraphInput,
    "recall_memory": RecallMemoryInput,
    "learn_baseline": LearnBaselineInput,
    "propose_candidate": ProposeCandidateInput,
    "finish": FinishInput,
}


class ReActStep(BaseModel):
    """LLM 이 매 스텝 내는 출력 — 딱 하나의 다음 행동."""

    thought: str = Field(description="이 행동을 고른 이유 — 감사(audit)용, 판정에는 쓰이지 않는다")
    action: ActionName
    action_input: dict = {}


def parse_action_input(action: str, raw: dict) -> BaseModel | str:
    """액션별 전용 스키마로 재검증한다. 실패하면 에러 문자열을 반환한다."""
    schema = _INPUT_SCHEMAS.get(action)
    if schema is None:
        return f"알 수 없는 액션: {action!r}"
    try:
        return schema.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError 등 — LLM 재입력 유도용 메시지
        return f"{action} 입력 형식 오류: {exc}"
