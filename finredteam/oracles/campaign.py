"""Campaign/Objective 오라클 — 개별 불변식 위반들을 **조합**해 "공격자 목표가
달성됐는가"를 판정한다.

★ 왜 이게 red teaming 인가 (설계 변경돼도 재사용되는 핵심):
기존 `oracles/` 는 experiment 1건 → 불변식 1개를 본다 — 이건 취약점 분석에 가깝다.
이 계층은 한 단계 위에서, 한 run 에서 나온 **Finding 집합**을 입력으로 받아
"공격자의 목표(예: victim→attacker 무단 자금 유출)가 실제로 성립했는가"를 판정한다.
즉 kill-chain 조합 판정 — 개별 위반을 목표 관점으로 묶는다.

★ 순수성 (§설계원칙 4, §8 규칙 6): 이 모듈도 `oracles/` 의 순수 함수 규칙을
그대로 따른다 — llm/probing/network import 금지. `tests/test_oracles.py` 의
`test_oracles_are_pure_no_llm_or_network_imports` 가 `oracles/*.py` 전체를
소스 레벨로 강제하므로 이 파일도 자동으로 검사된다. 입력은 이미 판정이 끝난
`Finding` 과 `Principal` 목록뿐 — 네트워크도, LLM 도 없다.

★ registry.run_all 과 같은 철학: 등록된 목표를 매번 예외 없이 전부 돌린다
(`OBJECTIVES` 전체). 각 목표는 자기 판정 재료(해당 불변식 위반 Finding)가 없으면
스스로 UNKNOWN 을 낸다 — LLM 이 "이번엔 이 목표만 본다" 로 범위를 좁혀 불리한
판정을 피하는 경로 자체가 없다. candidate 가 오라클을 못 줄이는 것과 같은 이유로,
어떤 계층도 목표 집합을 줄일 수 없다.

판정 의미 (`Verdict` 재사용 — scorer/reporting 이 그대로 이해한다):
  VIOLATION = 목표 달성(objective achieved) — 완결된 공격 체인
  ANOMALY   = 부분 달성 / 발판 확보(foothold) — 체인 일부만 성립
  PASS      = 목표 미달성(관련 위반은 있으나 목표 조건 불충족)
  UNKNOWN   = 판정 재료 부족(관련 불변식 위반 Finding 자체가 없음)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel

from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import Finding
from finredteam.money import tol


class ObjectiveOutcome(BaseModel):
    """상위(목표) 판정 1건. `OracleOutcome` 의 목표 계층 대응물."""

    objective: str  # "OBJ-FUNDS-EXFIL" 등
    title: str  # 사람이 읽는 목표 이름
    verdict: Verdict
    reason: str  # ★ 코드가 생성한 수치 기반 문장만 (§18 조건 12) — LLM 문장 금지
    chain: list[str] = []  # 목표를 구성한 불변식 id 들 (kill-chain 구성 요소)
    evidence: dict[str, Any] = {}


# ── 판정 헬퍼 (순수) ──────────────────────────────────────────────────


def _account_by_principal(principals: list[Principal]) -> dict[str, str]:
    """principal_id → 계좌번호. balance_delta 의 키는 계좌번호 문자열이다
    (`StateDelta.compute`)."""
    return {p.id: str(p.account_number) for p in principals if p.account_number}


def _attacker_account_for(finding: Finding, pmap: dict[str, str]) -> str | None:
    """이 Finding 을 **실행한 주체**(candidate.principal_id)의 계좌가 공격자 계좌다.

    ★ 이 프로젝트는 authorized 시험이라 victim 도 에이전트가 로그인하는 principal 이다
    — 그래서 '공격자 계좌'를 전역 집합으로 잡으면 안 되고, 각 Finding 을 실제로 실행한
    주체의 계좌만 공격자 계좌로 본다. 나머지 principal 계좌는 target(피해자)으로 취급해
    유출 방향(누가 이득을 봤나)을 옳게 판정한다.
    """
    return pmap.get(finding.candidate.principal_id)


def _violations(findings: list[Finding], invariant: str) -> list[Finding]:
    """해당 불변식으로 VIOLATION 을 낸 Finding 들만 추린다."""
    return [
        f for f in findings if f.verdict == Verdict.VIOLATION and f.invariant == invariant
    ]


def _attacker_credited(finding: Finding, pmap: dict[str, str]) -> bool:
    """이 Finding 의 관측 델타에서 실행 주체(공격자)의 계좌가 유의미하게 증가했는가."""
    acct = _attacker_account_for(finding, pmap)
    delta = finding.result.delta
    if acct is None or delta is None:
        return False
    money = delta.balance_delta.get(acct)
    return money is not None and money.amount > tol(money.currency)


def _other_debited(finding: Finding, pmap: dict[str, str]) -> bool:
    """공격자 외 계좌가 유의미하게 감소했는가 (= 값의 출처가 관측됨)."""
    acct = _attacker_account_for(finding, pmap)
    delta = finding.result.delta
    if delta is None:
        return False
    for account, money in delta.balance_delta.items():
        if str(account) != acct and money.amount < -tol(money.currency):
            return True
    return False


def _attacker_gain_amount(finding: Finding, pmap: dict[str, str]) -> Decimal:
    """공격자 계좌 순증가(양수). evidence 표기용."""
    acct = _attacker_account_for(finding, pmap)
    delta = finding.result.delta
    if acct is None or delta is None:
        return Decimal(0)
    money = delta.balance_delta.get(acct)
    if money is not None and money.amount > 0:
        return money.amount
    return Decimal(0)


# ── 목표 판정기들 ────────────────────────────────────────────────────


def check_obj_funds_exfil(
    findings: list[Finding], principals: list[Principal]
) -> ObjectiveOutcome:
    """OBJ-FUNDS-EXFIL — 무단 자금 유출: 관측된 다른 계좌에서 공격자 계좌로 값이 이동.

    완결 체인(VIOLATION): INV-1B(무단 가치 흐름) 위반이면서, 그 델타가 공격자 계좌
    증가 + 다른 계좌 감소를 함께 보인다 = 자금이 실제로 공격자에게 넘어왔고 출처도 관측됨.
    발판(ANOMALY): INV-2(권한 경계) 위반으로 victim 객체 접근에는 성공했으나 아직
    자금 이동은 관측되지 않음 = 체인의 앞 단계만 성립.
    """
    pmap = _account_by_principal(principals)
    flow_viols = _violations(findings, "INV-1B")

    completed = [
        f
        for f in flow_viols
        if _attacker_credited(f, pmap) and _other_debited(f, pmap)
    ]
    if completed:
        gained = sum((_attacker_gain_amount(f, pmap) for f in completed), Decimal(0))
        return ObjectiveOutcome(
            objective="OBJ-FUNDS-EXFIL",
            title="무단 자금 유출 (victim→attacker)",
            verdict=Verdict.VIOLATION,
            reason=(
                f"INV-1B 위반 {len(completed)}건에서 공격자 계좌 순증가 +{gained} 와 "
                f"타 계좌 감소가 함께 관측됨 — 무단 자금 이동 완결"
            ),
            chain=["INV-1B"] + (["INV-2"] if _violations(findings, "INV-2") else []),
            evidence={
                "completed_findings": [f.candidate.id for f in completed],
                "attacker_gain": str(gained),
            },
        )

    access_viols = _violations(findings, "INV-2")
    if access_viols:
        return ObjectiveOutcome(
            objective="OBJ-FUNDS-EXFIL",
            title="무단 자금 유출 (victim→attacker)",
            verdict=Verdict.ANOMALY,
            reason=(
                f"INV-2 권한경계 위반 {len(access_viols)}건으로 victim 객체 접근에는 "
                f"성공했으나, 공격자 계좌로의 자금 이동은 아직 관측되지 않음 — 발판 확보(체인 미완결)"
            ),
            chain=["INV-2"],
            evidence={"access_findings": [f.candidate.id for f in access_viols]},
        )

    return ObjectiveOutcome(
        objective="OBJ-FUNDS-EXFIL",
        title="무단 자금 유출 (victim→attacker)",
        verdict=Verdict.UNKNOWN,
        reason="INV-1B/INV-2 위반 Finding 이 없어 자금 유출 목표를 판정할 재료가 없음",
    )


def check_obj_value_forge(
    findings: list[Finding], principals: list[Principal]
) -> ObjectiveOutcome:
    """OBJ-VALUE-FORGE — 무에서 가치 창출: 대응하는 출처 감소 없이 공격자 잔액 증가.

    완결(VIOLATION): INV-1A(가치 보존)/INV-4(멱등성)/INV-1C(동시성) 중 하나가 위반이고
    공격자 계좌는 증가했는데 관측된 다른 계좌의 감소는 없다 = 값이 생성됨(유출과 구분).
    부분(ANOMALY): 위 불변식이 위반이나 공격자 순증가를 확증하지 못함(관측 부족 등).
    """
    pmap = _account_by_principal(principals)
    forge_invs = ("INV-1A", "INV-4", "INV-1C")
    viols = [f for inv in forge_invs for f in _violations(findings, inv)]
    if not viols:
        return ObjectiveOutcome(
            objective="OBJ-VALUE-FORGE",
            title="가치 창출(무에서 유) — 공격자 잔액 증식",
            verdict=Verdict.UNKNOWN,
            reason="INV-1A/INV-4/INV-1C 위반 Finding 이 없어 가치 창출 목표를 판정할 재료가 없음",
        )

    minted = [
        f
        for f in viols
        if _attacker_credited(f, pmap) and not _other_debited(f, pmap)
    ]
    if minted:
        gained = sum((_attacker_gain_amount(f, pmap) for f in minted), Decimal(0))
        chain = sorted({f.invariant for f in minted if f.invariant})
        return ObjectiveOutcome(
            objective="OBJ-VALUE-FORGE",
            title="가치 창출(무에서 유) — 공격자 잔액 증식",
            verdict=Verdict.VIOLATION,
            reason=(
                f"{'/'.join(chain)} 위반 {len(minted)}건에서 대응하는 타 계좌 감소 없이 "
                f"공격자 계좌 순증가 +{gained} 관측 — 가치가 생성됨(유출과 구분)"
            ),
            chain=chain,
            evidence={
                "minted_findings": [f.candidate.id for f in minted],
                "attacker_gain": str(gained),
            },
        )

    chain = sorted({f.invariant for f in viols if f.invariant})
    return ObjectiveOutcome(
        objective="OBJ-VALUE-FORGE",
        title="가치 창출(무에서 유) — 공격자 잔액 증식",
        verdict=Verdict.ANOMALY,
        reason=(
            f"{'/'.join(chain)} 위반은 있으나 공격자 계좌 순증가를 확증하지 못함 "
            f"(관측 부족 또는 이득 귀속 불명) — 부분 성립"
        ),
        chain=chain,
        evidence={"violation_findings": [f.candidate.id for f in viols]},
    )


def _single_invariant_objective(
    objective: str, title: str, invariant: str, achieved_reason: str, unknown_reason: str
) -> Callable[[list[Finding], list[Principal]], ObjectiveOutcome]:
    """하나의 불변식 위반이 곧 목표 달성인 유형(권한상승·PII유출 등)의 공용 판정기.

    조합이 필요 없는 목표라도, 목표 어휘를 단일 계층으로 통일하기 위해 여기서 함께
    다룬다 — 보고/채점이 '취약점'이 아니라 '공격자가 무엇을 달성했나'로 읽히게 한다.
    """

    def _check(findings: list[Finding], principals: list[Principal]) -> ObjectiveOutcome:
        viols = _violations(findings, invariant)
        if viols:
            return ObjectiveOutcome(
                objective=objective,
                title=title,
                verdict=Verdict.VIOLATION,
                reason=f"{achieved_reason} ({invariant} 위반 {len(viols)}건)",
                chain=[invariant],
                evidence={"findings": [f.candidate.id for f in viols]},
            )
        return ObjectiveOutcome(
            objective=objective,
            title=title,
            verdict=Verdict.UNKNOWN,
            reason=unknown_reason,
        )

    return _check


check_obj_authz_bypass = _single_invariant_objective(
    "OBJ-AUTHZ-BYPASS",
    "권한 경계 우회 — victim 객체 무단 접근",
    "INV-2",
    "entitlement 없는 객체 접근에 성공",
    "INV-2 위반 Finding 이 없어 권한 경계 우회 목표를 판정할 재료가 없음",
)

check_obj_priv_esc = _single_invariant_objective(
    "OBJ-PRIV-ESC",
    "권한 상승 — 관리자/특권 기능 획득",
    "INV-BFLA",
    "일반 사용자가 특권 기능 접근에 성공(mass assignment 등)",
    "INV-BFLA 위반 Finding 이 없어 권한 상승 목표를 판정할 재료가 없음",
)

check_obj_pii_exfil = _single_invariant_objective(
    "OBJ-PII-EXFIL",
    "민감정보 유출 — 카드정보 과다 노출",
    "INV-9",
    "마스킹 없는 전체 PAN+CVV 노출",
    "INV-9 위반 Finding 이 없어 민감정보 유출 목표를 판정할 재료가 없음",
)


# ── 목표 레지스트리 + 전량 실행 ────────────────────────────────────────

ObjectiveFn = Callable[[list[Finding], list[Principal]], ObjectiveOutcome]

OBJECTIVES: dict[str, ObjectiveFn] = {
    "OBJ-FUNDS-EXFIL": check_obj_funds_exfil,
    "OBJ-VALUE-FORGE": check_obj_value_forge,
    "OBJ-AUTHZ-BYPASS": check_obj_authz_bypass,
    "OBJ-PRIV-ESC": check_obj_priv_esc,
    "OBJ-PII-EXFIL": check_obj_pii_exfil,
}


def run_all_objectives(
    findings: list[Finding], principals: list[Principal]
) -> list[ObjectiveOutcome]:
    """등록된 목표를 예외 없이 전부 판정한다 — 어떤 계층도 이 목록을 줄일 수 없다.

    입력 `findings` 는 한 run 의 Finding 전체(또는 VIOLATION 집합)를 그대로 넘기면
    된다 — 각 판정기가 필요한 불변식 위반만 내부에서 골라 쓰고, 없으면 UNKNOWN 을 낸다.
    """
    return [fn(findings, principals) for fn in OBJECTIVES.values()]
