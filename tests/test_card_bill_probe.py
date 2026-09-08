"""card_limit / bill_limit 프로브 → INV-5 스파인 테스트.

가짜 vuln-bank(카드 한도·청구 금액 무검증 재현)를 httpx.MockTransport 로 흉내내고,
각 프로브 → registry.run_all → INV-5 판정까지 결정적으로 이어지는지 못박는다.
"""
from __future__ import annotations

import json

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import (
    Engagement, EngagementActions, EngagementScope, EngagementTarget,
)
from finredteam.models.core import Principal, Verdict
from finredteam.oracles.verifier import verify
from finredteam.probing.finance import bill
from finredteam.probing.finance import card_limit as card_limit_probe
from finredteam.probing.session import Session
from finredteam.targets.loader import load_target

OWNER = Principal(id="attacker_A", username="atk", password="x", account_number="111", token="t-a")


def _policy():
    eng = Engagement(
        engagement_id="cb-test",
        target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
    )
    return compile_policy(eng)


def _card_session():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p == "/api/virtual-cards/create":
            body = json.loads(request.content or b"{}")
            lim = float(body.get("card_limit", 1000.0))  # 무검증: 음수도 그대로 확정
            return httpx.Response(200, json={
                "status": "success",
                "card_details": {"id": 1, "limit": lim, "balance": 0},
            })
        if request.method == "POST" and p == "/api/virtual-cards/1/update-limit":
            body = json.loads(request.content or b"{}")
            lim = float(body.get("card_limit", 0))  # mass assignment: 그대로 확정
            return httpx.Response(200, json={
                "status": "success",
                "debug_info": {"card_details": {"id": 1, "card_limit": lim}},
            })
        return httpx.Response(404, json={"status": "error"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_card_creation_negative_limit_flows_to_inv5_violation():
    candidate, result = card_limit_probe.run_card_creation_limit_probe(_card_session(), OWNER, card_limit="-500.00")
    assert result.policy_signals["accepted"] is True
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"
    assert candidate.path == "/api/virtual-cards/create"


def test_card_update_negative_limit_flows_to_inv5_violation():
    candidate, result = card_limit_probe.run_card_update_limit_probe(_card_session(), OWNER, "1", card_limit="-1.00")
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"
    assert candidate.path == "/api/virtual-cards/1/update-limit"


def _bill_session(*, min_amount=10.0, max_amount=None):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "GET" and p == "/api/bill-categories":
            return httpx.Response(200, json={"status": "success", "categories": [{"id": 3, "name": "Utilities"}]})
        if request.method == "GET" and p == "/api/billers/by-category/3":
            return httpx.Response(200, json={"status": "success", "billers": [
                {"id": 7, "name": "PowerCo", "minimum_amount": min_amount, "maximum_amount": max_amount},
            ]})
        if request.method == "POST" and p == "/api/bill-payments/create":
            return httpx.Response(200, json={"status": "success", "payment_details": {"amount": -100.0}})
        return httpx.Response(404, json={"status": "error"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_bill_discover_biller_returns_server_declared_bounds():
    biller = bill.discover_biller(_bill_session(min_amount=25.0, max_amount=5000.0))
    assert biller == {"id": 7, "minimum_amount": 25.0, "maximum_amount": 5000.0}


def test_bill_negative_amount_flows_to_inv5_violation():
    session = _bill_session()
    biller = bill.discover_biller(session)
    candidate, result = bill.run_bill_amount_policy_probe(session, OWNER, biller=biller, amount="-100.00")
    assert result.policy_signals["accepted"] is True
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"
    assert candidate.path == "/api/bill-payments/create"


def test_bill_below_server_minimum_flows_to_inv5_violation():
    # 서버 선언 최소 50 인데 0.50 결제가 수락됨 → INV-5 최소 위반 (F-23)
    session = _bill_session(min_amount=50.0)
    biller = bill.discover_biller(session)
    candidate, result = bill.run_bill_amount_policy_probe(session, OWNER, biller=biller, amount="0.50")
    assert result.policy_signals["min"] == "50.0"
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"
    assert "최소" in finding.reason
