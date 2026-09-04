"""planning/planner.py 의 expected_delta 계산 — Day5 의 핵심 분기점.

네트워크 없이, 미리 만들어둔 BaselineRecord 로 "authorized 방향은 baseline 그대로
고정하고, 크기만 요청 파라미터에 비례시킨다"는 규칙을 고정한다. §18 예시
(amount=-500 인데 expected 는 attacker -500 / victim +500)를 그대로 재현한다.
"""
from __future__ import annotations

from decimal import Decimal

from finredteam.knowledge.baseline import BaselineRecord, BaselineRegistry
from finredteam.money import Currency, Money
from finredteam.planning.candidates import CandidateSkeleton
from finredteam.planning.planner import _expected_delta_for, build_candidate

USD = Currency(code="USD", minor_unit=2)


def _baselines_with_transfer_10() -> BaselineRegistry:
    registry = BaselineRegistry()
    registry.register(
        BaselineRecord(
            ref_id="bl_transfer_01",
            op="transfer",
            path="/transfer",
            principal_id="attacker_A",
            deltas={
                "ATTACKER": Money.parse("-10.00", USD),
                "VICTIM": Money.parse("10.00", USD),
            },
            roles={"ATTACKER": "payer", "VICTIM": "payee"},
            normal_params={"amount": 10, "to_account": "VICTIM"},
        )
    )
    return registry


def _skeleton() -> CandidateSkeleton:
    return CandidateSkeleton(
        layer="finance",
        operation="F2",
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
        subject_account="ATTACKER",
        watch_accounts=["ATTACKER", "VICTIM"],
        needs_baseline=True,
        hint={"op": "transfer", "counterparty_account": "VICTIM"},
    )


def test_expected_delta_keeps_baseline_direction_for_negative_amount():
    baselines = _baselines_with_transfer_10()
    exp = _expected_delta_for(_skeleton(), {"amount": "-500.00", "to_account": "VICTIM"}, baselines)

    assert exp is not None
    assert exp.provenance == "baseline_observed"
    assert exp.baseline_ref == "bl_transfer_01"
    # §18 예시와 동일: 부호가 아니라 크기(|−500|/10=50배)만 반영하고, 방향은 baseline 그대로.
    assert exp.deltas["ATTACKER"].amount == Decimal("-500.00")
    assert exp.deltas["VICTIM"].amount == Decimal("500.00")


def test_expected_delta_scales_with_positive_amount_too():
    baselines = _baselines_with_transfer_10()
    exp = _expected_delta_for(_skeleton(), {"amount": "25.00", "to_account": "VICTIM"}, baselines)

    assert exp is not None
    assert exp.deltas["ATTACKER"].amount == Decimal("-25.00")
    assert exp.deltas["VICTIM"].amount == Decimal("25.00")


def test_expected_delta_none_without_baseline():
    empty = BaselineRegistry()
    exp = _expected_delta_for(_skeleton(), {"amount": "-500.00"}, empty)
    assert exp is None


def test_expected_delta_none_when_skeleton_does_not_need_baseline():
    baselines = _baselines_with_transfer_10()
    skeleton = _skeleton()
    skeleton.needs_baseline = False
    assert _expected_delta_for(skeleton, {"amount": "-500.00"}, baselines) is None


def test_expected_delta_uses_discovered_amount_field_name_not_hardcoded_amount():
    # 실측 버그 회귀 가드: _UNIT_PARAM_BY_OP 가 "amount"를 하드코딩하고 있었는데,
    # Operation Discovery(recon/operation_discovery.py)가 다른 이름(예: "value")을
    # 찾아낸 대상에서는 그 필드가 params 에 있어도 무시되어 expected_delta 가
    # 항상 None 이 되는 문제가 있었다. baseline 자체도 discovery 로 학습된 것이므로
    # normal_params 도 discovered 필드명("value")을 쓴다(§ agent/react.py::
    # discover_and_learn_money_operation 이 실제로 만드는 모양과 동일).
    baselines = BaselineRegistry()
    baselines.register(
        BaselineRecord(
            ref_id="bl_transfer_discovered",
            op="transfer",
            path="/api/v2/send-money",
            principal_id="attacker_A",
            deltas={"ATTACKER": Money.parse("-10.00", USD), "VICTIM": Money.parse("10.00", USD)},
            roles={"ATTACKER": "payer", "VICTIM": "payee"},
            normal_params={"value": 10, "dest": "VICTIM"},
        )
    )
    skeleton = CandidateSkeleton(
        layer="finance",
        operation="F2",
        principal_id="attacker_A",
        method="POST",
        path="/api/v2/send-money",
        subject_account="ATTACKER",
        watch_accounts=["ATTACKER", "VICTIM"],
        needs_baseline=True,
        hint={"op": "transfer", "counterparty_account": "VICTIM", "amount_field": "value", "recipient_field": "dest"},
    )
    # "amount" 키는 아예 없다 — discovered 필드명(value)만 있다.
    exp = _expected_delta_for(skeleton, {"value": "-500.00", "dest": "VICTIM"}, baselines)

    assert exp is not None
    assert exp.deltas["ATTACKER"].amount == Decimal("-500.00")
    assert exp.deltas["VICTIM"].amount == Decimal("500.00")


def test_expected_delta_remaps_to_a_different_principals_accounts():
    # 회귀 테스트: baseline 은 attacker_A(-> ATTACKER)/victim_V(-> VICTIM) 로 실측됐지만,
    # 이번 후보는 다른 주체(victim_V 가 발신자, merchant_M 이 수신자)다. 원본 계좌번호를
    # 그대로 재사용하면 엉뚱한 계좌에 대한 expected_delta 가 나와 오탐(false VIOLATION)을
    # 낸다 — 역할(payer/payee) 기준으로 이번 후보의 실제 계좌에 다시 매핑되어야 한다.
    baselines = _baselines_with_transfer_10()
    skeleton = CandidateSkeleton(
        layer="finance",
        operation="F2",
        principal_id="victim_V",
        method="POST",
        path="/transfer",
        subject_account="VICTIM_AS_SENDER",
        watch_accounts=["VICTIM_AS_SENDER", "MERCHANT"],
        needs_baseline=True,
        hint={"op": "transfer", "counterparty_account": "MERCHANT"},
    )
    exp = _expected_delta_for(skeleton, {"amount": "7.00", "to_account": "MERCHANT"}, baselines)

    assert exp is not None
    assert set(exp.deltas) == {"VICTIM_AS_SENDER", "MERCHANT"}
    assert exp.deltas["VICTIM_AS_SENDER"].amount == Decimal("-7.00")
    assert exp.deltas["MERCHANT"].amount == Decimal("7.00")
    # 원본 baseline 계좌(ATTACKER/VICTIM)가 새 결과에 섞여 들어오면 안 된다.
    assert "ATTACKER" not in exp.deltas
    assert "VICTIM" not in exp.deltas


def test_build_candidate_forces_recipient_to_validated_target():
    """회귀(거짓양성): LLM이 params.to_account에 target과 다른 계좌를 넣어도, 실제
    요청은 검증·감시된 target 계좌로 강제돼야 한다.

    실측 버그(runs/20260904-165450): watch_accounts는 target(=counterparty)로
    만드는데 요청은 params.to_account로 나가, 둘이 어긋나면 수신 계좌가 관측되지
    않아 INV-1A가 '값이 사라졌다'는 거짓 위반을 5건 냈다. 값은 실제로 보존됐다.
    """
    baselines = _baselines_with_transfer_10()
    skeleton = _skeleton()  # hint counterparty_account="VICTIM", watch=[ATTACKER, VICTIM]
    cand = build_candidate(baselines=baselines, skeleton=skeleton, rationale="",
                           params={"amount": "1.00", "to_account": "SOMEONE_UNWATCHED"})
    # 요청은 감시하는 VICTIM으로 강제됨 — 수신 측이 반드시 관측되어 INV-1A 거짓양성이 사라진다
    assert cand.params["to_account"] == "VICTIM"
    assert "VICTIM" in cand.watch_accounts
