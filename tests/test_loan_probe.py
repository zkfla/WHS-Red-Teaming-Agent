"""loan_limit 프로브 → INV-5 스파인 테스트.

가짜 vuln-bank(app.py::request_loan 재현: amount 검증 없이 수락)를 httpx.MockTransport
로 흉내내고, run_loan_amount_policy_probe → registry.run_all → INV-5 판정까지 한 줄로
이어지는지 못박는다. 네트워크·서버 불필요, 결정적.
"""
from __future__ import annotations

import json

import httpx

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import (
    Engagement, EngagementActions, EngagementScope, EngagementTarget,
)
from finredteam.models.core import Principal, Verdict
from finredteam.oracles import registry
from finredteam.oracles.verifier import verify
from finredteam.probing.finance.loan import run_loan_amount_policy_probe
from finredteam.probing.session import Session
from finredteam.targets.loader import load_target

OWNER = Principal(id="attacker_A", username="atk", password="x", account_number="111", token="t-attacker")


def _policy():
    eng = Engagement(
        engagement_id="loan-test",
        target=EngagementTarget(name="vulnbank", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
    )
    return compile_policy(eng)


def _session(*, validates: bool):
    """validates=False → vuln-bank 재현(amount 무검증 수락). True → 음수/0 거부하는 안전 서버."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/request_loan":
            body = json.loads(request.content or b"{}")
            amount = float(body.get("amount", 0))
            if validates and amount <= 0:
                return httpx.Response(400, json={"status": "error", "message": "invalid amount"})
            return httpx.Response(200, json={"status": "success", "message": "Loan requested successfully"})
        return httpx.Response(404, json={"status": "error"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_negative_loan_accepted_flows_to_inv5_violation():
    session = _session(validates=False)
    candidate, result = run_loan_amount_policy_probe(session, OWNER, amount="-500.00")
    assert result.policy_signals["accepted"] is True
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-5"
    assert "양수성" in finding.reason


def test_zero_loan_accepted_flows_to_inv5_violation():
    session = _session(validates=False)
    candidate, result = run_loan_amount_policy_probe(session, OWNER, amount="0.00")
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"


def test_oversized_loan_with_asserted_max_flows_to_inv5_violation():
    session = _session(validates=False)
    candidate, result = run_loan_amount_policy_probe(
        session, OWNER, amount="100000000.00", max_policy="1000000.00"
    )
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION and finding.invariant == "INV-5"
    assert "최대" in finding.reason


def test_negative_loan_rejected_is_not_violation():
    # 안전 서버(음수 거부)면 accepted=false → INV-5 는 위반이 아님(PASS)
    session = _session(validates=True)
    candidate, result = run_loan_amount_policy_probe(session, OWNER, amount="-500.00")
    assert result.policy_signals["accepted"] is False
    outcomes = registry.run_all(candidate, result)
    inv5 = next(o for o in outcomes if o.invariant == "INV-5")
    assert inv5.verdict == Verdict.PASS


def test_probe_endpoint_matches_ground_truth():
    # 채점 매칭(scorer)이 성립하려면 candidate 의 method+path 가 GT 와 같아야 한다.
    session = _session(validates=False)
    candidate, _ = run_loan_amount_policy_probe(session, OWNER, amount="-1.00")
    assert candidate.method == "POST" and candidate.path == "/request_loan"
    assert candidate.operation == "request_loan"
