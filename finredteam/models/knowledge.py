"""Fact, Lesson, Entitlement — memory/ 와 knowledge/graph.py 가 쓰는 타입."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Fact(BaseModel):
    """정찰·시험 중 관측되어 확립된 사실. `confidence="observed"` 만 판정에 쓴다."""

    subject: str
    predicate: str
    object: str
    confidence: Literal["observed", "inferred"] = "observed"
    source: str = ""
    evidence_ref: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Lesson(BaseModel):
    """실패/성공 시도에서 추출한 교훈. planning/prioritizer.py 가 다음 후보 순위에 반영."""

    candidate_summary: str
    outcome: str  # verdict 문자열 또는 실행 오류 요약
    insight: str  # "GET /accounts/{id} 타 주체 요청은 403" 처럼 재사용 가능한 관찰
    attack_class: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Entitlement(BaseModel):
    """정상 경로에서 확정된 '가져야 할 권한' — observed_* 와 절대 섞이지 않는다 (§5)."""

    principal: str
    relation: Literal[
        "entitled_own",
        "entitled_read",
        "entitled_write",
        "entitled_transfer_from",
        "entitled_approve",
    ]
    obj: str
    source: Literal["provisioned", "self_created", "explicit_owner_field", "normal_flow"]
    evidence_ref: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
