"""잔액 상태 관측 — targets/configs/*.yaml 의 `observation` 설정을 읽어
`Session` 으로 실제 요청을 보내고 `ObservedState` 를 만든다.

관측에 실패한 필드는 값을 지어내지 않고 `ObservedState.missing` 에 이름만
남긴다 — 오라클이 이를 보고 `UNKNOWN` 을 낸다 (§4 규칙 5).
"""
from __future__ import annotations

from finredteam.jsonpath import get_path
from finredteam.models.core import Principal
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Money
from finredteam.probing.session import Session, SessionError
from finredteam.targets.loader import TargetConfig


def observe_balance(session: Session, principal: Principal, target: TargetConfig) -> ObservedState:
    """`observation.balance` 설정으로 principal 본인 계좌 잔액을 관측한다."""
    state = ObservedState(principal_id=principal.id)
    obs = target.observation_config()
    cfg = obs.get("balance") if isinstance(obs, dict) else None

    if not cfg or not principal.account_number:
        state.missing.add("balance")
        return state

    path = cfg["path"].replace("{account_number}", principal.account_number)
    method = cfg.get("method", "GET")
    try:
        if cfg.get("auth_required", True):
            ex = session.authed_request(method, path, principal)
        else:
            ex = session.anonymous_request(method, path)
    except SessionError:
        state.missing.add("balance")
        return state

    if ex.status_code >= 400 or ex.response_body is None:
        state.missing.add("balance")
        return state

    value_path = cfg.get("value_path", "$.balance")
    raw = get_path(ex.response_body, value_path)
    if raw is None:
        state.missing.add("balance")
        return state

    currency = target.adapter.default_currency
    state.balances[principal.account_number] = Money.parse(raw, currency)
    return state


def observe_account_balance(
    session: Session, account_number: str, target: TargetConfig, *, principal_id: str
) -> ObservedState:
    """임의 계좌번호의 잔액을 관측한다 (BOLA 시험 등 — 본인 계좌가 아닐 수 있다).

    관측 자체는 인증 없이 가능하더라도, 이 값을 `entitled_*` 판단에 쓰지 않는다(§5) —
    이 함수는 순수하게 "관측"만 하고, 권한 판단은 knowledge/graph.py 의 몫이다.
    """
    state = ObservedState(principal_id=principal_id)
    obs = target.observation_config()
    cfg = obs.get("balance") if isinstance(obs, dict) else None
    if not cfg:
        state.missing.add("balance")
        return state

    path = cfg["path"].replace("{account_number}", account_number)
    try:
        ex = session.anonymous_request(cfg.get("method", "GET"), path)
    except SessionError:
        state.missing.add("balance")
        return state

    if ex.status_code >= 400 or ex.response_body is None:
        state.missing.add("balance")
        return state

    raw = get_path(ex.response_body, cfg.get("value_path", "$.balance"))
    if raw is None:
        state.missing.add("balance")
        return state

    state.balances[account_number] = Money.parse(raw, target.adapter.default_currency)
    return state


def observe_many(
    session: Session, target: TargetConfig, accounts: list[str], *, principal_id: str
) -> ObservedState:
    """여러 계좌 잔액을 한 스냅샷으로 합친다 — INV-1A/1B 는 송신자·수신자를 함께 봐야 한다.

    `knowledge/baseline.py`, `probing/executor.py` 가 공유해서 쓴다.
    """
    combined = ObservedState(principal_id=principal_id)
    for account in accounts:
        obs = observe_account_balance(session, account, target, principal_id=principal_id)
        combined.balances.update(obs.balances)
        combined.missing |= obs.missing
    return combined


def compute_delta(before: ObservedState, after: ObservedState) -> StateDelta:
    return StateDelta.compute(before, after)
