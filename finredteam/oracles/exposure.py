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
from finredteam.oracles.base import OracleOutcome, anomaly, is_success_status, passed, unknown, violation


def check_inv_9(candidate: CandidateFinding, result: ExecutionResult) -> OracleOutcome:
    signals = result.exposure_signals
    if not signals:
        return unknown("INV-9", "노출 신호 없음 — 이 실행은 민감정보 노출 프로브가 아니다")

    if not any(is_success_status(code) for code in result.status_codes):
        return passed("INV-9", "성공 응답이 없어 노출이 발생하지 않음")

    pan = signals.get("full_pan_exposed", False)
    cvv = signals.get("cvv_exposed", False)

    if pan and cvv:
        return violation(
            "INV-9",
            "카드정보 과다 노출: 마스킹 없는 전체 PAN 과 CVV 가 응답에 함께 노출됨",
            evidence={"full_pan_exposed": True, "cvv_exposed": True},
        )
    if pan:
        return violation(
            "INV-9",
            "카드정보 노출: 마스킹 없는 전체 PAN 이 응답에 노출됨",
            evidence={"full_pan_exposed": True},
        )
    if cvv:
        return anomaly(
            "INV-9",
            "CVV 가 응답에 노출됨 (전체 PAN 은 미확인)",
            evidence={"cvv_exposed": True},
        )
    return passed("INV-9", "마스킹 없는 PAN/CVV 노출 신호 없음")
