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


def build_candidate(
    skeleton: CandidateSkeleton,
    params: dict[str, str],
    rationale: str,
    baselines: BaselineRegistry,
) -> CandidateFinding:
    expected_delta = _expected_delta_for(skeleton, params, baselines)
    if expected_delta is None and skeleton.needs_baseline:
        expected_delta = ExpectedDelta(
            deltas={}, provenance="llm_inferred", note="baseline 미확보 — 판정 불가"
        )

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
        rationale=rationale,
    )
