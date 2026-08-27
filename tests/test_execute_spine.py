"""B 스파인 통합 테스트 — "한 사이클이 안 끊긴다"의 회귀 가드.

개별 조각(resolve_skeleton / build_candidate / oracles / verifier)은 각자 단위
테스트가 있지만, 이 파일은 그것들을 **한 줄로 이어붙인 실행 경로**가 예외 없이
Finding 까지 도달하는지를 못박는다:

    propose_hypothesis(operation, target)
      → resolve_skeleton            (검증 + 요청 뼈대)
      → build_candidate             (baseline 로 expected_delta 조립)
      → executor.run                (가짜 세션으로 실제 요청 N회 + 전후 관측)
      → verifier.verify             (등록된 오라클 전체 실행 → 최종 verdict)

네트워크·API키·실제 vuln-bank 없이 결정적으로 돈다 (httpx.MockTransport 로 대상
서버를 흉내낸다). 새 operation(charge 등)을 붙일 때 이 하네스에 케이스만 추가하면
"사이클 유지"가 자동으로 확인된다.
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import Engagement, EngagementActions, EngagementScope, EngagementTarget
from finredteam.knowledge.baseline import BaselineRecord, BaselineRegistry
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Principal, Verdict
from finredteam.models.knowledge import Entitlement
from finredteam.money import Currency, Money
from finredteam.planning.candidates import resolve_skeleton
from finredteam.planning.planner import build_candidate
from finredteam.probing.executor import run as execute
from finredteam.probing.session import Session
from finredteam.oracles.verifier import verify
from finredteam.targets.loader import load_target

USD = Currency(code="USD", minor_unit=2)
ATTACKER_ACCT = "111"
VICTIM_ACCT = "222"


def _policy():
    engagement = Engagement(
        engagement_id="spine-test",
        target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
    )
    return compile_policy(engagement)


def _mock_target_session() -> Session:
    """vuln-bank 를 흉내내는 상태 있는 가짜 세션.

    - GET /check_balance/<acct>  → 현재 잔액 (auth 불필요, vulnbank.yaml 실측대로)
    - POST /transfer             → ★ vuln-bank 의 부호 미방어 버그 재현:
        `balance -= amount; recipient += amount` 를 부호 그대로 적용한다.
        amount=-500 이면 발신자가 오히려 +500, 수신자가 -500 이 된다.
    """
    balances = {ATTACKER_ACCT: Decimal("1000.00"), VICTIM_ACCT: Decimal("1000.00")}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/check_balance/"):
            acct = path.rsplit("/", 1)[-1]
            if acct not in balances:
                return httpx.Response(404, json={"error": "no such account"})
            return httpx.Response(200, json={"balance": str(balances[acct]), "username": f"user_{acct}"})

        if request.method == "POST" and path == "/transfer":
            body = json.loads(request.content or b"{}")
            amount = Decimal(str(body["amount"]))
            to_acct = str(body["to_account"])
            sender = ATTACKER_ACCT  # 이 테스트에선 공격자만 송금한다
            if balances[sender] >= abs(amount):  # 서버의 (허술한) 잔액 체크
                balances[sender] -= amount       # ★ 부호 그대로 — 여기가 버그
                balances[to_acct] += amount
            return httpx.Response(200, json={"new_balance": str(balances[sender]), "status": "success"})

        return httpx.Response(404, json={"error": "unknown endpoint"})

    target = load_target("vulnbank")
    return Session(target, _policy(), _transport=httpx.MockTransport(handler))


def _seed_baseline() -> BaselineRegistry:
    """정상 송금(amount=10) 실측을 미리 등록해둔다 — INV-1B 는 이 baseline_observed
    기대값과의 비교로만 VIOLATION 을 낸다(§6). 여기선 실측 대신 직접 주입한다."""
    registry = BaselineRegistry()
    registry.register(
        BaselineRecord(
            ref_id="bl_transfer_01",
            op="transfer",
            path="/transfer",
            principal_id="attacker_A",
            deltas={ATTACKER_ACCT: Money.parse("-10.00", USD), VICTIM_ACCT: Money.parse("10.00", USD)},
            roles={ATTACKER_ACCT: "payer", VICTIM_ACCT: "payee"},
            normal_params={"amount": 10, "to_account": VICTIM_ACCT},
        )
    )
    return registry


def _principals() -> list[Principal]:
    return [
        Principal(id="attacker_A", username="a", password="pw", account_number=ATTACKER_ACCT, token="tok"),
        Principal(id="victim_V", username="v", password="pw", account_number=VICTIM_ACCT, token="tok2"),
    ]


def _graph(principals: list[Principal]) -> KnowledgeGraph:
    graph = KnowledgeGraph()
    graph.add_entitlements(
        [
            Entitlement(principal="attacker_A", relation="entitled_own", obj=f"account:{ATTACKER_ACCT}", source="self_created"),
            Entitlement(principal="victim_V", relation="entitled_own", obj=f"account:{VICTIM_ACCT}", source="self_created"),
        ]
    )
    return graph


def test_negative_transfer_flows_through_spine_to_violation():
    """음수 송금이 스파인 전체를 예외 없이 통과해 VIOLATION 으로 확정된다."""
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    # ① 가설 검증 + 뼈대 (propose_hypothesis 상당)
    skeleton = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    assert not isinstance(skeleton, str), f"resolve_skeleton 이 에러 문자열을 냄: {skeleton}"

    # ② baseline 로 expected_delta 조립 (LLM 이 고른 amount=-500)
    candidate = build_candidate(skeleton, {"amount": "-500.00", "to_account": VICTIM_ACCT, "description": "x"}, "", baselines)
    assert candidate.expected_delta is not None
    assert candidate.expected_delta.provenance == "baseline_observed"

    # ③ 실제 실행 (execute_experiment 상당) — 가짜 대상에 요청 + 전후 관측
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    assert all(code < 400 for code in result.status_codes), f"요청 실패: {result.status_codes}"

    # ④ 판정 (등록된 오라클 전체 실행)
    finding = verify(candidate, result, graph)

    # 사이클이 안 끊기고 실제 판정까지 도달했다 (UNKNOWN 이 아니라 확정 verdict)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant in ("INV-1B", "INV-6")
    # 부호 반전 실측: 공격자 +500, 피해자 -500
    assert result.delta is not None
    assert result.delta.balance_delta[ATTACKER_ACCT].amount == Decimal("500.00")
    assert result.delta.balance_delta[VICTIM_ACCT].amount == Decimal("-500.00")
