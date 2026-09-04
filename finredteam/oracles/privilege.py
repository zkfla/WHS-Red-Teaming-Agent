"""INV-BFLA — 함수 수준 인가(Broken Function Level Authorization) / 권한 상승.

일반 사용자가 접근할 수 없어야 하는 관리자 기능에, 클라이언트가 통제해서는
안 되는 필드(예: 회원가입 시 is_admin)를 주입(mass assignment)해 접근에 성공하면
위반이다(§ GT G-04 Registration Mass Assignment, G-05 Admin Data Access).

★ 순수성(§설계원칙 4): 프로브가 만든 boolean 신호(result.authz_signals)만 본다.
판정 재료는 "정상 사용자는 거부됐는가"와 "주입한 사용자는 접근에 성공했는가"의
비교 결과지, 응답 원문이 아니다.
"""
from __future__ import annotations

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import OracleOutcome, passed, unknown, violation


def check_bfla(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    signals = result.authz_signals
    if not signals:
        return unknown("INV-BFLA", "권한상승 신호 없음 — 이 실행은 권한상승 프로브가 아니다")

    escalated = signals.get("privilege_escalation_confirmed", False)
    if escalated:
        return violation(
            "INV-BFLA",
            "권한 상승: 일반 사용자는 거부(403)된 관리자 기능에, mass assignment(is_admin 주입)로 "
            "가입한 사용자가 접근에 성공(200)함",
            evidence={"privilege_escalation_confirmed": True},
        )
    return passed("INV-BFLA", "권한 상승 신호 없음 (관리자 기능 접근 경계 정상)")
