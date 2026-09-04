"""CandidateFinding → ExecutionResult → Finding.

★ expected_delta.provenance 가 "baseline_observed"/"adapter_declared"/"spec_derived"
가 아니면(=llm_inferred 등) 오라클은 이 값을 판정 근거로 쓰지 않는다 (§6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from finredteam.models.core import Verdict
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Money

Layer = Literal["generic", "finance"]

# 오라클 판정에 실제로 쓸 수 있는 출처. "llm_inferred" 는 힌트일 뿐 판정에는 쓰지 않는다.
TRUSTED_PROVENANCE = ("baseline_observed", "adapter_declared", "spec_derived")


class ExpectedDelta(BaseModel):
    deltas: dict[str, Money] = {}
    provenance: Literal[
        "baseline_observed", "adapter_declared", "spec_derived", "llm_inferred"
    ]
    baseline_ref: str | None = None
    note: str = ""

    def is_trusted(self) -> bool:
        return self.provenance in TRUSTED_PROVENANCE


class CandidateFinding(BaseModel):
    """실행 전에 오라클을 선언한 후보 (§설계원칙 3).

    ★ 오라클 선언은 **후보가 하지 않는다** — LLM 이 "이건 F1 이니까 INV-2 만
    본다" 는 식으로 오라클 범위를 스스로 좁히면, 그 좁힘 자체가 판정을
    왜곡할 수 있다(예: 부호 반전이 우연히 INV-6 로 잡히는데 후보가 INV-1B 만
    선언했다면 놓친다). 대신 **시스템이 실행 전에 고정해둔 오라클 전체
    집합**(`oracles/registry.py::REGISTRY`+`GRAPH_REGISTRY`)을 매 실행마다
    예외 없이 전부 돌린다 — 이게 "실행 전 선언"을 이번 설계에서 더 강하게
    지키는 방법이다: 후보마다 다르게 정할 수 있는 게 아니라 애초에 고정.
    """

    id: str
    layer: Layer
    operation: str  # "read_balance", "transfer" 등 — 어떤 메커니즘으로 요청을 만들지
    principal_id: str
    method: str
    path: str
    params: dict[str, object] = {}
    headers: dict[str, str] = {}
    expected_delta: ExpectedDelta | None = None
    needs_baseline: bool = False
    subject_account: str | None = None  # oracles/concurrency.py 등에서 참조
    unit_amount: Money | None = None  # 동시성(INV-1C) 시험의 단위 금액
    logical_txn_id: str | None = None  # 멱등성(INV-4) 판정 기준 — 페이로드가 아니라 이 필드 값
    watch_accounts: list[str] = []  # 실행 전/후 관측해야 하는 계좌 (발신·수신 등)
    rationale: str = ""  # LLM 이 후보를 왜 골랐는지 — 판정에는 쓰이지 않는, 사람이 읽는 근거
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExecutionResult(BaseModel):
    """후보를 실제 실행한 결과. 판정 전 단계 — verifier.py 입력."""

    candidate: CandidateFinding
    status_codes: list[int] = []
    bodies: list[dict] = []  # storage/evidence.py 를 거친 뒤의 안전한 표현만 담는다
    before: ObservedState | None = None
    after: ObservedState | None = None
    delta: StateDelta | None = None
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_refs: list[str] = []
    # Stop Condition 감지용 신호 (agent/react.py::policy_stop_reason) — 판정에는
    # 쓰이지 않는다, 오라클은 여전히 status_codes/delta 만 본다.
    latencies_ms: list[float] = []
    sensitive_levels: list[str] = []
    response_texts: list[str] = []
    # INV-9(민감정보 노출) 판정용 파생 신호 — 프로브(probing/finance/card_exposure.py)가
    # 원문(PAN/CVV 값)을 보고 "마스킹 없는 전체 PAN이 있었나" 같은 boolean 만
    # 뽑아 여기 담는다. 오라클(oracles/exposure.py)은 이 boolean 만 보고 판정하므로
    # 민감 원문이 오라클/저장소로 새지 않는다(§10, §설계원칙 6).
    exposure_signals: dict[str, bool] = {}


class Finding(BaseModel):
    candidate: CandidateFinding
    result: ExecutionResult
    verdict: Verdict
    invariant: str | None = None  # 실제로 판정을 낸 오라클 id
    reason: str = ""  # ★ 코드가 생성한 수치 기반 문장만. LLM 생성 문장 금지 (§18 조건 12)
    evidence_refs: list[str] = []

    @property
    def is_verified(self) -> bool:
        return self.verdict == Verdict.VIOLATION
