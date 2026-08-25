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
    """실행 전에 오라클을 선언한 후보 (§설계원칙 3)."""

    id: str
    layer: Layer
    attack_class: str  # "F2", "F4", "G-SQLI" 등 templates/attacks.yaml 의 id
    invariants: list[str]  # ["INV-1B", "INV-6"] 등 oracles/ 의 판정 대상
    principal_id: str
    method: str
    path: str
    params: dict[str, object] = {}
    headers: dict[str, str] = {}
    expected_delta: ExpectedDelta | None = None
    needs_baseline: bool = False
    subject_account: str | None = None  # oracles/concurrency.py 등에서 참조
    unit_amount: Money | None = None  # F4 동시성 시험의 단위 금액
    logical_txn_id: str | None = None  # F3 멱등성 판정 기준 — 페이로드가 아니라 이 필드 값
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
