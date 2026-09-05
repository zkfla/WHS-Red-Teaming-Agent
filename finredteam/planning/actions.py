"""ReAct 액션 어휘 — LLM 이 고를 수 있는 액션은 이 7개뿐이다.

자유 텍스트 경로·페이로드를 LLM 이 직접 쓰게 하지 않는다. 후보 생성(가설)과
실제 실행(실험)을 분리한다 — `propose_hypothesis` 는 부작용이 없고,
`execute_experiment` 만 실제로 요청을 보낸다. `execute_experiment` 의
`hypothesis_id`가 가리키는 오라클 판정은 LLM 이 정하지 않는다 —
`oracles/registry.py` 가 실행 전에 고정해둔 오라클 전체 집합을 매번 돈다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionName = Literal[
    "observe_graph",
    "recall_memory",
    "learn_baseline",
    "discover_surface",
    "propose_hypothesis",
    "execute_experiment",
    "run_probe",
    "finish",
]

ACTION_CATALOG = """\
- observe_graph: {} — 그래프 요약(알려진 계좌, entitled_own 관계, 발견된 엔드포인트)을 본다. 부작용 없음.
- recall_memory: {"query": "<검색어>"} — 이전 사실·교훈을 검색한다. 부작용 없음.
- learn_baseline: {"op": "<baseline_ops 의 op 이름, 예: transfer>"} — 정상 파라미터로
  한 번 실행해 실측 기준선을 만든다. 금액 관련 실험 전에 필요할 수 있다.
- discover_surface: {"path": "/추측하는-경로", "method": "GET|HEAD|OPTIONS"} — 아직
  모르는 경로를 안전하게(읽기 전용) 찔러본다. 존재하면 그래프에 새 사실로 남는다.
  응답 안에서 다른 경로 후보가 발견되면 그것도 같이 알려준다. 횟수 제한 있음.
- propose_hypothesis: {"operation": "read_balance|transfer", "principal_id": "<주체>",
  "target": "account:<계좌번호>", "params": {...}} — 아직 실행하지 않는다. 그래프에
  실존하는 대상인지만 검증하고 hypothesis_id 를 돌려준다. 부작용 없음.
- execute_experiment: {"hypothesis_id": "<propose_hypothesis 가 준 id>"} — 실제로
  요청을 보내고 판정까지 받는다. 어떤 invariant 를 검사할지는 LLM 이 정하지 않는다
  — 관련된 오라클이 자동으로 전부 돈다.
- run_probe: {"probe": "sensitive_exposure|privilege_escalation|card_fund|sqli_scan|jwt_forge|charge_idempotency|race_transfer"}
  — 계좌 조회·송금 외의 특수 취약점을, 필요한 셋업(계정/카드 생성 등)까지 코드가 자동으로
  하는 전용 프로브로 시험한다. sensitive_exposure=응답에 비밀번호/재설정 PIN/카드 PAN·CVV가
  노출되는지, privilege_escalation=회원가입 mass assignment(is_admin 주입)로 관리자
  권한이 상승되는지, card_fund=카드 충전 시 환율(exchange_rate) 조작으로 가치가
  창조되는지, sqli_scan=계좌/거래 조회 경로에 boolean-based SQL injection 이 통하는지,
  jwt_forge=흔한 약한 시크릿으로 위조한 JWT 를 서버가 수락하는지(토큰 위조),
  charge_idempotency=같은 merchant_order_id 로 결제를 반복하면 카드 잔액이 여러 번
  차감되는지(멱등성 부재), race_transfer=같은 잔액에 대해 송금을 동시에 여러 번 보내면
  잔액 검사를 우회해 초과 인출되는지(경쟁조건, 재현율 있게 여러 번 시행). 판정은
  오라클이 자동으로 낸다(INV-9/INV-BFLA/INV-1A/INV-4/INV-1C/G-SQLI/G-JWT 등).
- finish: {"reason": "<왜 그만두는지>"} — 탐색을 종료한다. 예산 한도가 항상 우선한다.
"""


class ObserveGraphInput(BaseModel):
    pass


class RecallMemoryInput(BaseModel):
    query: str = ""


class LearnBaselineInput(BaseModel):
    op: str


class DiscoverSurfaceInput(BaseModel):
    path: str
    method: Literal["GET", "HEAD", "OPTIONS"] = "GET"


class ProposeHypothesisInput(BaseModel):
    operation: str
    principal_id: str
    target: str = ""
    params: dict[str, str] = {}


class ExecuteExperimentInput(BaseModel):
    hypothesis_id: str


class RunProbeInput(BaseModel):
    probe: Literal[
        "sensitive_exposure", "privilege_escalation", "card_fund", "sqli_scan", "jwt_forge",
        "charge_idempotency", "race_transfer",
    ]


class FinishInput(BaseModel):
    reason: str = ""


_INPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "observe_graph": ObserveGraphInput,
    "recall_memory": RecallMemoryInput,
    "learn_baseline": LearnBaselineInput,
    "discover_surface": DiscoverSurfaceInput,
    "propose_hypothesis": ProposeHypothesisInput,
    "execute_experiment": ExecuteExperimentInput,
    "run_probe": RunProbeInput,
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
