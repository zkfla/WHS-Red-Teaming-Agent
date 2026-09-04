"""skeleton + LLM 이 고른 파라미터 → CandidateFinding.

★ expected_delta 는 LLM 출력에서 가져오지 않는다. LLM 이 고른 파라미터(예: amount)를
`knowledge/baseline.py` 의 실측 기준선에 선형으로 대입해서 계산한다 — LLM 은
"어떤 값을 시도할지"만 정하고, "그 값이면 무엇이 정상인지"는 baseline 이 정한다(§6).

"스켈레톤 일괄 refine"(LLM 이 파라미터도 골라줌) 대신, LLM 이 이미
`propose_hypothesis` 액션 입력으로 파라미터까지 고른 채로 들어온다 — 이 모듈은
그 파라미터를 검증된 skeleton 에 꽂아 CandidateFinding 을 만드는 순수 조립 단계다.
오라클(어떤 invariant 를 검사할지)은 여기서 정하지 않는다 — `oracles/registry.py`
가 실행 후 전체 집합을 돈다.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from finredteam.knowledge.baseline import BaselineRegistry
from finredteam.models.finding import CandidateFinding, ExpectedDelta
from finredteam.money import Money
from finredteam.planning.candidates import CandidateSkeleton

_UNIT_PARAM_BY_OP = {"transfer": "amount"}


def _expected_delta_for(
    skeleton: CandidateSkeleton, params: dict[str, str], baselines: BaselineRegistry
) -> ExpectedDelta | None:
    """authorized(baseline) 방향은 고정하고, 크기만 요청 파라미터에 비례시킨다.

    예: baseline 이 amount=10 으로 attacker -10 / victim +10 을 실측했다면,
    후보의 amount=-500 에 대해 "인가된" 기대값은 여전히 attacker -500 / victim +500
    이다(§18 예시) — 부호는 이 서버의 버그가 만드는 것이지 authorized 시맨틱이
    아니므로, scale 은 부호가 아니라 **크기 비율**(|chosen| / |baseline 요청값|)로
    계산한다.
    """
    if not skeleton.needs_baseline:
        return None
    op = skeleton.hint.get("op")
    if not op or not baselines.has(op):
        return None

    # ★ Operation Discovery(recon/operation_discovery.py)가 찾은 실제 필드명을
    # 우선한다 — 대상마다 "amount"가 아닐 수 있다. 발견 못 했을 때만(LEGACY_
    # BOOTSTRAP 정적 config 경로) 이 코드베이스의 관례값 "amount"로 폴백한다.
    unit_param = skeleton.hint.get("amount_field") or _UNIT_PARAM_BY_OP.get(op)
    if not unit_param or unit_param not in params:
        return None

    record = baselines.get(op)
    if record is None:
        return None

    baseline_raw = record.normal_params.get(unit_param)
    if baseline_raw is None:
        return None

    try:
        baseline_amount = Decimal(str(baseline_raw))
        chosen = Decimal(str(params[unit_param]))
    except InvalidOperation:
        return None
    if baseline_amount == 0:
        return None

    scale = abs(chosen) / abs(baseline_amount)

    # ★ baseline 은 특정 두 계좌(payer/payee)로 실측됐지만, 이번 후보는 다른 주체가
    # 보낼 수도 있다. 원본 계좌번호를 그대로 재사용하면 "누가 보냈는지"가 틀어져
    # 엉뚱한 expected_delta 가 나온다 — 역할(payer/payee)로 이번 후보의 실제 계좌에
    # 다시 매핑한다.
    account_by_role: dict[str, str] = {}
    if skeleton.subject_account:
        account_by_role["payer"] = skeleton.subject_account
    counterparty_account = skeleton.hint.get("counterparty_account")
    if counterparty_account:
        account_by_role["payee"] = counterparty_account

    return baselines.derive_expected(op, scale, account_by_role=account_by_role)


def _unit_amount_for(
    skeleton: CandidateSkeleton, params: dict[str, str], expected_delta: ExpectedDelta | None
) -> Money | None:
    """INV-1C(동시성) 판정용 단위 금액 — 요청 1건의 크기를 부호 없는 Money 로.

    통화는 expected_delta(=baseline 실측)에서 가져온다 — baseline 이 없으면 통화를
    확정할 수 없어 None 이고, 그 경우 oracles/concurrency.py 가 UNKNOWN 을 낸다.
    금액 필드명은 Operation Discovery 가 찾은 것(hint.amount_field)을 우선한다.
    """
    if expected_delta is None or not expected_delta.deltas:
        return None
    op = skeleton.hint.get("op")
    unit_param = skeleton.hint.get("amount_field") or _UNIT_PARAM_BY_OP.get(op or "")
    if not unit_param or unit_param not in params:
        return None
    currency = next(iter(expected_delta.deltas.values())).currency
    try:
        return abs(Money.parse(params[unit_param], currency))
    except (InvalidOperation, ValueError):
        return None


def build_candidate(
    skeleton: CandidateSkeleton,
    params: dict[str, str],
    rationale: str,
    baselines: BaselineRegistry,
) -> CandidateFinding:
    # ★ 수신 계좌를 검증된 target(skeleton.hint.counterparty_account)으로 강제한다.
    # 리졸버는 `target`(검증·감시하는 계좌)으로 watch_accounts 를 만드는데, 실제
    # 요청은 `params` 로 나간다 — LLM 이 params 의 to_account 에 target 과 다른 계좌를
    # 넣으면, 돈은 감시하지 않는 계좌로 가서 수신 측 변화가 관측되지 않고 INV-1A 가
    # "값이 사라졌다"는 거짓 위반을 낸다(실측: 값은 보존됨). 둘을 일치시켜 막는다.
    counterparty = skeleton.hint.get("counterparty_account")
    if counterparty:
        params = dict(params)
        recipient_field = skeleton.hint.get("recipient_field") or "to_account"
        params[recipient_field] = counterparty

    expected_delta = _expected_delta_for(skeleton, params, baselines)
    if expected_delta is None and skeleton.needs_baseline:
        expected_delta = ExpectedDelta(
            deltas={}, provenance="llm_inferred", note="baseline 미확보 — 판정 불가"
        )
    unit_amount = _unit_amount_for(skeleton, params, expected_delta)

    return CandidateFinding(
        id=f"{skeleton.operation}-{uuid.uuid4().hex[:8]}",
        layer=skeleton.layer,
        operation=skeleton.operation,
        principal_id=skeleton.principal_id,
        method=skeleton.method,
        path=skeleton.path,
        params=params,
        subject_account=skeleton.subject_account,
        watch_accounts=skeleton.watch_accounts,
        needs_baseline=skeleton.needs_baseline,
        expected_delta=expected_delta,
        unit_amount=unit_amount,
        rationale=rationale,
    )
