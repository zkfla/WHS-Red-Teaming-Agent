"""정상 동작 기준선 ★ — expected_delta 의 유일하게 신뢰되는 출처(§6).

공격 전에 `baseline_ops`(targets/configs/*.yaml)에 적힌 정상 파라미터로 실제
연산을 한 번 실행해 상태 변화를 실측해둔다. 오라클(oracles/value.py 등)은
이 실측값(provenance="baseline_observed")과의 비교로만 VIOLATION 을 낸다 —
LLM 이 제안한 expected_delta(provenance="llm_inferred")는 힌트일 뿐 판정에 쓰이지
않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from finredteam.jsonpath import get_path
from finredteam.models.core import Principal
from finredteam.models.finding import ExpectedDelta
from finredteam.money import Money
from finredteam.probing.finance.state import compute_delta, observe_many
from finredteam.probing.session import Session, SessionError
from finredteam.targets.loader import TargetConfig


@dataclass
class BaselineRecord:
    ref_id: str
    op: str
    path: str
    principal_id: str
    deltas: dict[str, Money]
    roles: dict[str, str] = field(default_factory=dict)  # account_number -> "payer"|"payee" 등
    normal_params: dict[str, object] = field(default_factory=dict)
    response_extra: dict[str, str] = field(default_factory=dict)


class BaselineRegistry:
    """공격 전에 '정상 연산'을 한 번 실행해 상태 변화를 실측해둔다."""

    def __init__(self) -> None:
        self._records: dict[str, BaselineRecord] = {}
        self._latest_ref_by_op: dict[str, str] = {}

    def learn(
        self,
        session: Session,
        target: TargetConfig,
        op_cfg: dict,
        principal: Principal,
        *,
        counterparty: Principal | None = None,
    ) -> BaselineRecord | None:
        """`op_cfg`(baseline_ops 원소 하나)를 정상 파라미터로 실행하고 실측 델타를 기록한다.

        요청/관측이 실패하거나(§4 규칙 5) 관측에 missing 이 섞이면 기록하지 않고
        `None` 을 반환한다 — 이 경우 오라클은 이후에도 계속 UNKNOWN 을 낸다.
        """
        op = op_cfg["op"]
        method = op_cfg.get("method", "POST")
        path = op_cfg["path"]
        params: dict[str, object] = dict(op_cfg.get("normal_params", {}))

        if counterparty is not None and counterparty.account_number:
            params = {
                key: value.replace("{victim_account}", counterparty.account_number)
                if isinstance(value, str)
                else value
                for key, value in params.items()
            }
        if principal.account_number:
            path = path.replace("{account_number}", principal.account_number)

        accounts = [a for a in (principal.account_number, getattr(counterparty, "account_number", None)) if a]
        if not accounts:
            return None

        before = observe_many(session, target, accounts, principal_id=principal.id)
        try:
            ex = session.authed_request(method, path, principal, json_body=params)
        except SessionError:
            return None
        if ex.status_code >= 400:
            return None
        after = observe_many(session, target, accounts, principal_id=principal.id)

        delta = compute_delta(before, after)
        if delta.has_missing() or not delta.balance_delta:
            return None

        response_extra: dict[str, str] = {}
        for key, jsonpath in op_cfg.get("response_paths", {}).items():
            value = get_path(ex.response_body, jsonpath)
            if value is not None:
                response_extra[key] = str(value)

        roles: dict[str, str] = {}
        if principal.account_number:
            roles[principal.account_number] = "payer"
        if counterparty is not None and counterparty.account_number:
            roles[counterparty.account_number] = "payee"

        ref_id = f"bl_{op}_{len(self._records) + 1:02d}"
        record = BaselineRecord(
            ref_id=ref_id,
            op=op,
            path=path,
            principal_id=principal.id,
            deltas=delta.balance_delta,
            roles=roles,
            normal_params=params,
            response_extra=response_extra,
        )
        self._records[ref_id] = record
        self._latest_ref_by_op[op] = ref_id
        return record

    def register(self, record: BaselineRecord) -> None:
        """미리 만들어둔 BaselineRecord 를 등록한다 (테스트, 저장된 기록 복원용)."""
        self._records[record.ref_id] = record
        self._latest_ref_by_op[record.op] = record.ref_id

    def has(self, op: str) -> bool:
        return op in self._latest_ref_by_op

    def get(self, op: str) -> BaselineRecord | None:
        ref_id = self._latest_ref_by_op.get(op)
        return self._records.get(ref_id) if ref_id else None

    def derive_expected(
        self,
        op: str,
        scale: Decimal | int | None = None,
        *,
        account_by_role: dict[str, str] | None = None,
    ) -> ExpectedDelta | None:
        """기준선이 있으면 파라미터에 비례해 기대 델타를 계산한다.

        `scale`: baseline 실측 대비 배율. 예를 들어 baseline 이 amount=10 으로 실측한
        델타를, 후보의 amount=-500 에 맞춰 비교하려면 scale=Decimal(50) 을 넘긴다
        (부호가 아니라 크기 비율 — planner.py 참고). 생략하면(=1배) baseline 델타를
        그대로 쓴다.

        `account_by_role`: 이 후보의 실제 계좌가 baseline 을 실측할 때와 다른
        주체(principal)일 때 필요하다 — baseline 은 "payer"/"payee" 같은 역할별로
        기록되어 있으므로(§ `roles`), 이 맵으로 역할을 이번 후보의 실제 계좌번호로
        치환한다. 생략하면 baseline 을 실측한 바로 그 계좌에 대해서만 스케일링한다.

        기준선이 없으면 `None` — 오라클은 이를 보고 UNKNOWN 을 낸다. 에이전트는
        여기서 "환율"이나 "정답"을 계산하지 않는다 — 실측값에 선형 비례만 적용한다.
        """
        record = self.get(op)
        if record is None:
            return None
        factor = scale if scale is not None else Decimal(1)

        if account_by_role is None:
            deltas = {account: money * factor for account, money in record.deltas.items()}
            return ExpectedDelta(deltas=deltas, provenance="baseline_observed", baseline_ref=record.ref_id)

        deltas = {}
        for account, money in record.deltas.items():
            role = record.roles.get(account)
            target_account = account_by_role.get(role) if role else None
            if target_account is None:
                continue
            deltas[target_account] = money * factor
        if not deltas:
            return None
        return ExpectedDelta(deltas=deltas, provenance="baseline_observed", baseline_ref=record.ref_id)
