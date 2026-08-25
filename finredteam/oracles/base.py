"""오라클 공통 타입 — 판정 4종만 낸다 (§설계원칙 4).

★ 이 패키지(`oracles/`)의 모든 모듈은 순수 함수다: LLM도, 네트워크도 호출하지
않는다(§8 규칙 6). `tests/test_oracles.py`의 `test_oracles_are_pure`가 이를
소스 레벨(import 문)로 강제한다 — 위반하면 테스트가 즉시 실패한다.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from finredteam.models.core import Verdict


class OracleOutcome(BaseModel):
    verdict: Verdict
    invariant: str
    reason: str  # ★ 수치 기반 문장만. LLM 생성 문장 금지 (§18 조건 12)
    evidence: dict[str, Any] = {}


def passed(invariant: str, reason: str = "") -> OracleOutcome:
    return OracleOutcome(verdict=Verdict.PASS, invariant=invariant, reason=reason)


def violation(invariant: str, reason: str, evidence: dict[str, Any] | None = None) -> OracleOutcome:
    return OracleOutcome(
        verdict=Verdict.VIOLATION, invariant=invariant, reason=reason, evidence=evidence or {}
    )


def anomaly(invariant: str, reason: str, evidence: dict[str, Any] | None = None) -> OracleOutcome:
    return OracleOutcome(
        verdict=Verdict.ANOMALY, invariant=invariant, reason=reason, evidence=evidence or {}
    )


def unknown(invariant: str, reason: str) -> OracleOutcome:
    return OracleOutcome(verdict=Verdict.UNKNOWN, invariant=invariant, reason=reason)


def is_success_status(code: int) -> bool:
    return 200 <= code < 300
