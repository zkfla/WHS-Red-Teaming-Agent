"""INV-9 — 민감정보 과다 노출. 응답에 마스킹 없는 전체 PAN + CVV 가 함께
노출되면 위반(PCI-DSS 관점의 카드정보 과다 노출, § GT F-16).

★ 순수성(§설계원칙 4): 이 오라클은 민감 원문을 절대 받지 않는다. 프로브
(probing/finance/card_exposure.py)가 원문을 보고 뽑아낸 **파생 boolean 신호**
(`result.exposure_signals`)만 판정 재료로 쓴다 — 값이 아니라 "그런 값이
있었는가"만 본다. 신호가 없으면 UNKNOWN(프로브가 안 만들었을 뿐, 정상이라는
뜻이 아니다).
"""
from __future__ import annotations

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import OracleOutcome, is_success_status, passed, unknown, violation


# 신호 이름 → 사람이 읽는 노출 설명. 프로브(probing/finance/*)가 이 이름으로
# boolean 을 세팅하면 오라클이 그대로 위반 사유로 쓴다.
_EXPOSURE_LABELS = {
    "full_pan_exposed": "마스킹 없는 전체 PAN",
    "cvv_exposed": "카드 CVV",
    "password_echoed": "요청에 보낸 비밀번호 원문이 응답에 그대로 반환됨",
    "reset_pin_exposed": "비밀번호 재설정 PIN",
}


def check_inv_9(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    signals = result.exposure_signals
    if not signals:
        return unknown("INV-9", "노출 신호 없음 — 이 실행은 민감정보 노출 프로브가 아니다")

    if not any(is_success_status(code) for code in result.status_codes):
        return passed("INV-9", "성공 응답이 없어 노출이 발생하지 않음")

    exposed = [name for name, present in signals.items() if present]
    if exposed:
        labels = [_EXPOSURE_LABELS.get(name, name) for name in exposed]
        return violation(
            "INV-9",
            "민감정보 노출: " + ", ".join(labels) + " 이(가) 응답에 노출됨",
            evidence={name: True for name in exposed},
        )
    return passed("INV-9", "민감정보 노출 신호 없음")
