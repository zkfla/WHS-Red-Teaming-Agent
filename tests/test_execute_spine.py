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
import threading
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
from finredteam.probing.finance.burst import run_race_candidate, summarize_race
from finredteam.probing.finance.auth_exposure import run_forgot_password_pin_probe, run_register_password_echo_probe
from finredteam.probing.finance.card_exposure import run_card_exposure_probe
from finredteam.probing.finance.privesc import run_admin_escalation_probe
from finredteam.probing.finance.card_fund import run_card_fund_manipulation
from finredteam.probing.finance.charge import run_charge_idempotency_trial
from finredteam.probing.session import Session
from finredteam.oracles import registry
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

        if request.method == "GET" and path.startswith("/transactions/"):
            acct = path.rsplit("/", 1)[-1]
            if acct not in balances:
                return httpx.Response(404, json={"error": "no such account"})
            return httpx.Response(200, json={"account_number": acct, "transactions": []})

        if request.method == "GET" and path == "/api/transactions":
            acct = request.url.params.get("account_number")
            if acct not in balances:
                return httpx.Response(404, json={"error": "no such account"})
            return httpx.Response(200, json={"account_number": acct, "transactions": []})

        if request.method == "POST" and path == "/api/virtual-cards/create":
            return httpx.Response(200, json={"status": "success", "card_details": {"id": 42}})

        if request.method == "POST" and path == "/api/virtual-cards/42/toggle-freeze":
            return httpx.Response(200, json={"status": "success", "message": "Card frozen successfully"})

        if request.method == "GET" and path == "/api/virtual-cards/42/transactions":
            return httpx.Response(200, json={"status": "success", "transactions": []})

        if request.method == "POST" and path == "/api/virtual-cards/42/update-limit":
            return httpx.Response(200, json={"status": "success", "message": "Card updated successfully"})

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


def _race_mock_session() -> Session:
    """lost-update 경쟁조건을 흉내내는 가짜 세션.

    관측(GET /check_balance) 시점의 잔액을 스냅샷해두고, 동시에 들어온 모든
    POST /transfer 가 그 스냅샷에서 계산·기록한다 — 그래서 N건이 전부 성공(200)해도
    실제 차감은 1건분만 남는다(고전적 lost update). 실제 vuln-bank(단일스레드 Flask)는
    이걸 재현 못 하지만, 우리 탐지 메커니즘(burst + INV-1C)이 도는지는 이렇게
    결정적으로 검증할 수 있다.
    """
    balances = {ATTACKER_ACCT: Decimal("100000.00"), VICTIM_ACCT: Decimal("100000.00")}
    snapshots: dict[str, Decimal] = {}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/check_balance/"):
            acct = path.rsplit("/", 1)[-1]
            if acct not in balances:
                return httpx.Response(404, json={"error": "no such account"})
            with lock:
                snapshots[acct] = balances[acct]  # 버스트 직전 잔액을 스냅샷
                return httpx.Response(200, json={"balance": str(balances[acct]), "username": f"user_{acct}"})

        if request.method == "POST" and path == "/transfer":
            body = json.loads(request.content or b"{}")
            amount = Decimal(str(body["amount"]))
            to_acct = str(body["to_account"])
            sender = ATTACKER_ACCT
            with lock:
                # ★ 모든 동시 요청이 같은 스냅샷에서 기록 → 최종 차감은 1건분만 반영
                balances[sender] = snapshots.get(sender, balances[sender]) - amount
                balances[to_acct] = snapshots.get(to_acct, balances[to_acct]) + amount
            return httpx.Response(200, json={"new_balance": str(balances[sender]), "status": "success"})

        return httpx.Response(404, json={"error": "unknown endpoint"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_concurrent_transfer_flows_through_spine_to_race_violation():
    """동시 송금이 스파인(resolve→build→burst→INV-1C)을 예외 없이 통과해
    재현율 있는 VIOLATION 으로 확정된다."""
    session = _race_mock_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    assert not isinstance(skeleton, str)

    candidate = build_candidate(skeleton, {"amount": "100.00", "to_account": VICTIM_ACCT, "description": "x"}, "", baselines)
    # ★ 이번 수정의 핵심: unit_amount 가 채워져야 INV-1C 가 UNKNOWN 을 벗어난다.
    assert candidate.unit_amount is not None
    assert candidate.unit_amount.amount == Decimal("100.00")

    attacker = principals[0]
    # trials 를 낮춰 테스트를 빠르게 유지한다(결정적 목이라 매 시행이 동일하게 VIOLATION).
    trials = run_race_candidate(
        session, target, candidate, attacker, watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT], n_concurrent=5, trials=2
    )
    summary = summarize_race(trials)

    # 사이클이 안 끊기고 재현율 있는 VIOLATION 까지 도달했다
    assert summary["judged"] >= 1
    assert summary["violations"] >= 1
    assert summary["reproduction_rate"] > 0


def test_bola_read_of_unentitled_account_flows_to_inv2_violation():
    """공격자가 권한 없는 타 계좌(victim)를 조회 → INV-2 VIOLATION 으로 확정된다.

    코드 변경 없이 read_balance 리졸버 + INV-2(그래프 오라클) + entitled_* 그래프가
    이어져 BOLA 익스 클래스가 스파인으로 확정되는지 검증한다.
    """
    session = _mock_target_session()  # GET /check_balance/<acct> 는 인증 무관하게 잔액 반환(BOLA)
    target = session.target
    principals = _principals()
    graph = _graph(principals)  # attacker→111, victim→222 (attacker 는 222 에 권한 없음)
    baselines = _seed_baseline()

    # attacker 가 victim(222) 계좌를 조회한다 — 자기 것이 아니다
    skeleton = resolve_skeleton("read_balance", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    assert not isinstance(skeleton, str), skeleton
    assert skeleton.subject_account == VICTIM_ACCT

    candidate = build_candidate(skeleton, {}, "", baselines)
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=candidate.watch_accounts)
    assert all(code < 400 for code in result.status_codes)

    finding = verify(candidate, result, graph)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-2"


def test_bola_read_transactions_of_unentitled_account_flows_to_inv2_violation():
    """공격자가 권한 없는 타 계좌의 거래내역을 조회 → INV-2 VIOLATION.

    실제 vuln-bank 소스(app.py::get_transaction_history)에 "No authentication
    required (BOLA)"라고 명시된 취약점 — read_transactions 리졸버가 read_balance와
    같은 패턴(그래프 검증 + 기존 INV-2 재사용)으로 스파인에 이어지는지 검증한다.
    """
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("read_transactions", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    assert not isinstance(skeleton, str), skeleton
    assert skeleton.path == f"/transactions/{VICTIM_ACCT}"

    candidate = build_candidate(skeleton, {}, "", baselines)
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=candidate.watch_accounts)
    assert all(code < 400 for code in result.status_codes)

    finding = verify(candidate, result, graph)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-2"


def test_bola_api_transactions_of_unentitled_account_flows_to_inv2_violation():
    """인증된 공격자가 GET /api/transactions?account_number=<victim> 로 타 계좌
    거래내역을 조회 → INV-2 VIOLATION. 실제 소스(app.py::api_transactions)에
    "No validation of account_number parameter"로 명시된 취약점."""
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("api_transactions", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    assert not isinstance(skeleton, str), skeleton
    assert skeleton.path == f"/api/transactions?account_number={VICTIM_ACCT}"

    candidate = build_candidate(skeleton, {}, "", baselines)
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=candidate.watch_accounts)
    assert all(code < 400 for code in result.status_codes)

    finding = verify(candidate, result, graph)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-2"


def test_bola_card_toggle_freeze_of_unentitled_card_flows_to_inv2_violation():
    """공격자가 권한 없는 타인 카드의 freeze 상태를 변경 → INV-2 VIOLATION.

    카드는 계좌와 다른 객체 타입("card:42")이지만, INV-2 오라클이 완전한
    객체 id를 그대로 받아 같은 방식으로 판정한다(§ oracles/authz.py 일반화 검증).
    실제 소스(app.py::toggle_card_freeze)에 'BOLA - no verification if card
    belongs to user'로 명시된 상태변경 취약점.
    """
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    # victim_V 가 카드 42 를 소유(entitled_own) — attacker_A 는 권한 없음
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="card:42", source="self_created")
    )
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("card_toggle_freeze", "attacker_A", "card:42", principals, graph, target)
    assert not isinstance(skeleton, str), skeleton

    candidate = build_candidate(skeleton, {}, "", baselines)
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=candidate.watch_accounts)
    assert all(code < 400 for code in result.status_codes)

    finding = verify(candidate, result, graph)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-2"


def test_bola_card_update_limit_of_unentitled_card_flows_to_inv2_violation():
    """공격자가 권한 없는 타인 카드의 limit 을 변경 → INV-2 VIOLATION.

    실제 소스(app.py::update_card_limit)에 'BOLA - no verification if card
    belongs to user'로 명시. 같은 핸들러가 mass assignment(임의 필드 허용)도
    갖고 있지만, 이 테스트는 BOLA 자체만 card_limit 필드로 확정한다.
    """
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    graph.add_entitlement(
        Entitlement(principal="victim_V", relation="entitled_own", obj="card:42", source="self_created")
    )
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("card_update_limit", "attacker_A", "card:42", principals, graph, target)
    assert not isinstance(skeleton, str), skeleton

    candidate = build_candidate(skeleton, {"card_limit": "500.00"}, "", baselines)
    attacker = principals[0]
    result = execute(session, target, candidate, attacker, watch_accounts=candidate.watch_accounts)
    assert all(code < 400 for code in result.status_codes)

    finding = verify(candidate, result, graph)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-2"


def _charge_mock_session() -> Session:
    """merchant 등록 + 카드 생성/충전 + 결제(멱등성 없음, 실제 vuln-bank 재현)를
    흉내내는 가짢 세션. 같은 merchant_order_id 로 결제해도 매번 카드 잔액이
    차감된다 — 실제 소스(merchant_payments.py) 의 'No idempotency' 버그 그대로."""
    card = {"id": 1, "card_number": "4111", "cvv": "123", "expiry_date": "12/30", "currency": "USD"}
    state = {"balance": Decimal("0.00")}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/merchants/register":
            return httpx.Response(200, json={"token": "mtok"})
        if request.method == "POST" and path == "/api/virtual-cards/create":
            return httpx.Response(200, json={"status": "success", "card_details": {**card, "balance": 0}})
        if request.method == "POST" and path == "/api/virtual-cards/1/fund":
            body = json.loads(request.content or b"{}")
            state["balance"] += Decimal(str(body["amount"]))
            return httpx.Response(200, json={"status": "success"})
        if request.method == "GET" and path == "/api/virtual-cards":
            return httpx.Response(200, json={"status": "success", "cards": [{**card, "balance": float(state["balance"])}]})
        if request.method == "POST" and path == "/api/v1/payments/charge":
            body = json.loads(request.content or b"{}")
            amount = Decimal(str(body["amount"]))
            # ★ merchant_order_id 를 아예 확인하지 않는다 — 멱등성 없음이 이 버그의 핵심.
            state["balance"] -= amount
            return httpx.Response(200, json={"status": "success", "payment": {"status": "completed"}})
        return httpx.Response(404, json={"error": "unknown endpoint"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_merchant_charge_replay_flows_to_inv4_violation():
    """같은 merchant_order_id 로 3번 결제 → 카드 잔액이 3회분 차감(멱등성 없음)
    → INV-4 VIOLATION. run_charge_idempotency_trial(burst.py 와 같은 급의
    전용 오케스트레이션)이 관측한 재료로 오라클이 정확히 판정하는지 검증한다."""
    session = _charge_mock_session()
    owner = Principal(id="attacker_A", username="cardowner", password="pw", account_number=ATTACKER_ACCT, token="tok")
    merchant_token = session.register_merchant("Shop", "shop@x.com", "Rt!pass123")
    assert merchant_token is not None

    card_id = session.create_virtual_card(owner)
    assert card_id is not None
    fund_ex = session.authed_request("POST", f"/api/virtual-cards/{card_id}/fund", owner, json_body={"amount": "300.00"})
    assert fund_ex.status_code == 200

    cards = session.get_own_cards(owner)
    card = next(c for c in cards if str(c["id"]) == card_id)

    candidate, result = run_charge_idempotency_trial(
        session, owner, card, merchant_token, order_id="ORDER-1", amount="50.00", repeats=3
    )
    assert result.status_codes == [200, 200, 200]
    assert result.delta.balance_delta[card_id].amount == Decimal("-150.00")  # 3회분 차감 — 위반 증거

    # 이 실험은 카드→가맹점(관측 안 함)으로 나가는 단방향 결제를 같은 order_id 로
    # 반복한 것이다. INV-1A(폐쇄계 보존)와 INV-1B(단일 인가분 기준)는 이 경우를
    # 자기 기준으로는 판정할 수 없어 정직하게 UNKNOWN 으로 defer 하고(각각 open_flow /
    # 반복 logical_txn_id 신호), 멱등성의 영역인 INV-4 만 VIOLATION 을 낸다.
    outcomes = registry.run_all(candidate, result)
    by_invariant = {o.invariant: o for o in outcomes}
    assert by_invariant["INV-4"].verdict == Verdict.VIOLATION
    assert "3회 성공" in by_invariant["INV-4"].reason
    assert by_invariant["INV-1A"].verdict == Verdict.UNKNOWN  # 열린 흐름 — 보존 판정 불가
    assert by_invariant["INV-1B"].verdict == Verdict.UNKNOWN  # 반복 논리거래 — INV-4 에 defer

    # verify()는 유일한 VIOLATION 인 INV-4 를 정확히 헤드라인으로 고른다.
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-4"


def _card_fund_mock_session() -> Session:
    """USD 카드 충전 시 클라이언트 exchange_rate 를 그대로 반영하는 목(실제
    vuln-bank 의 mass assignment 버그 재현). 계좌 USD 잔액과 카드 USD 잔액을
    각각 관측 가능하게 한다."""
    acct = {"balance": Decimal("1000.00")}
    card = {"balance": Decimal("0.00")}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/check_balance/"):
            return httpx.Response(200, json={"balance": str(acct["balance"]), "username": "owner"})
        if request.method == "GET" and path == "/api/virtual-cards":
            return httpx.Response(200, json={"cards": [{"id": 1, "balance": float(card["balance"]), "currency": "USD"}]})
        if request.method == "POST" and path == "/api/virtual-cards/1/fund":
            body = json.loads(request.content or b"{}")
            usd = Decimal(str(body["amount"]))
            rate = Decimal(str(body.get("exchange_rate", "1")))  # ★ 클라이언트 값을 그대로 씀 = 버그
            acct["balance"] -= usd
            card["balance"] += usd * rate
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404, json={"error": "unknown endpoint"})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_card_fund_exchange_rate_manipulation_flows_to_inv1a_violation():
    """USD 카드 충전 시 exchange_rate 를 조작 → 계좌에서 usd 만큼 빠지고 카드에
    usd×rate 만큼 들어옴 → 같은 USD 에서 값 창조 → INV-1A VIOLATION.

    새 오라클 없이 기존 INV-1A(통화별 가치 보존)로 잡힌다 — 계좌·카드 잔액을
    한 델타로 합쳐 관측하는 게 핵심(run_card_fund_manipulation)."""
    session = _card_fund_mock_session()
    owner = Principal(id="attacker_A", username="owner", password="pw", account_number=ATTACKER_ACCT, token="tok")

    candidate, result = run_card_fund_manipulation(
        session, owner, "1", usd_amount="100.00", exchange_rate="100"
    )
    # 계좌 -100, 카드 +10000 (100 × 100) — USD 총합 +9900 창조
    assert result.delta.balance_delta[ATTACKER_ACCT].amount == Decimal("-100.00")
    assert result.delta.balance_delta["card:1"].amount == Decimal("10000.00")

    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-1A"


def test_card_fund_normal_rate_is_value_neutral_no_violation():
    """정상 충전(exchange_rate=1, USD 카드)은 계좌→카드 이동일 뿐이라 USD 총합
    0 → INV-1A 통과. 거짓양성이 안 나는지 확인(대조군)."""
    session = _card_fund_mock_session()
    owner = Principal(id="attacker_A", username="owner", password="pw", account_number=ATTACKER_ACCT, token="tok")

    candidate, result = run_card_fund_manipulation(
        session, owner, "1", usd_amount="100.00", exchange_rate="1"
    )
    assert result.delta.balance_delta[ATTACKER_ACCT].amount == Decimal("-100.00")
    assert result.delta.balance_delta["card:1"].amount == Decimal("100.00")

    finding = verify(candidate, result)
    assert finding.verdict != Verdict.VIOLATION  # 값 보존 → 위반 아님


def test_card_exposure_full_pan_and_cvv_flows_to_inv9_violation():
    """카드 목록 응답에 마스킹 없는 전체 PAN + CVV 가 노출 → INV-9 VIOLATION.

    프로브가 원문에서 boolean(full_pan_exposed/cvv_exposed)만 뽑고, 신규 오라클
    INV-9 이 그 신호로만 판정한다 — 민감 원문은 오라클로 안 넘어간다."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/virtual-cards":
            return httpx.Response(200, json={"cards": [{
                "id": 1, "card_number": "4111111111111111", "cvv": "123", "balance": 0.0,
            }]})
        return httpx.Response(404, json={})

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    owner = Principal(id="attacker_A", username="owner", password="pw", account_number=ATTACKER_ACCT, token="tok")

    candidate, result = run_card_exposure_probe(session, owner)
    assert result.exposure_signals == {"full_pan_exposed": True, "cvv_exposed": True}

    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-9"


def test_card_exposure_masked_pan_no_violation():
    """마스킹된 PAN(****1111)만 있으면 INV-9 위반 아님 — 거짓양성 방지 대조군."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/virtual-cards":
            return httpx.Response(200, json={"cards": [{"id": 1, "card_number": "************1111", "balance": 0.0}]})
        return httpx.Response(404, json={})

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    owner = Principal(id="attacker_A", username="owner", password="pw", account_number=ATTACKER_ACCT, token="tok")

    candidate, result = run_card_exposure_probe(session, owner)
    assert result.exposure_signals == {"full_pan_exposed": False, "cvv_exposed": False}

    finding = verify(candidate, result)
    assert finding.verdict != Verdict.VIOLATION


def test_register_password_echo_flows_to_inv9_violation():
    """회원가입 응답이 요청에 보낸 비밀번호를 그대로 echo → INV-9 VIOLATION."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/register":
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"status": "success", "debug_data": {"raw_data": body}})
        return httpx.Response(404, json={})

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    candidate, result = run_register_password_echo_probe(session, "u1", "Secr3tP@ss!")
    assert result.exposure_signals == {"password_echoed": True}
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-9"


def test_forgot_password_pin_disclosure_flows_to_inv9_violation():
    """비밀번호 재설정 응답에 PIN 노출 → INV-9 VIOLATION (계정탈취 발판)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/forgot-password":
            return httpx.Response(200, json={"status": "success", "debug_info": {"pin": "687"}})
        return httpx.Response(404, json={})

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    candidate, result = run_forgot_password_pin_probe(session, "u1")
    assert result.exposure_signals == {"reset_pin_exposed": True}
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-9"


def _privesc_mock_session(admin_panel_checks_flag: bool = True) -> Session:
    """회원가입 시 is_admin 을 그대로 저장하고, 관리자 패널은 is_admin 계정만
    200(아니면 403)을 주는 실제 vuln-bank 동작 재현."""
    users: dict[str, bool] = {}  # token -> is_admin

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/register":
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"status": "success", "debug_data": {"is_admin": bool(body.get("is_admin"))}})
        if request.method == "POST" and path == "/login":
            body = json.loads(request.content or b"{}")
            # 목: username에 'atk'가 들어가면 is_admin 주입된 계정으로 간주
            is_admin = "atk" in body.get("username", "")
            tok = f"tok_{'admin' if is_admin else 'user'}_{len(users)}"
            users[tok] = is_admin
            return httpx.Response(200, json={"status": "success", "token": tok})
        if request.method == "GET" and path == "/sup3r_s3cr3t_admin":
            auth = request.headers.get("Authorization", "")
            tok = auth.replace("Bearer ", "")
            return httpx.Response(200 if users.get(tok) else 403, json={})
        return httpx.Response(404, json={})

    return Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))


def test_mass_assignment_admin_escalation_flows_to_bfla_violation():
    """is_admin=true 주입 가입 → 일반 사용자는 403인 관리자 패널에 200 접근 →
    INV-BFLA VIOLATION. 정상 사용자(대조군)는 접근 거부됨을 프로브가 함께 확인."""
    session = _privesc_mock_session()
    candidate, result = run_admin_escalation_probe(session)
    assert result.authz_signals == {"privilege_escalation_confirmed": True}
    finding = verify(candidate, result)
    assert finding.verdict == Verdict.VIOLATION
    assert finding.invariant == "INV-BFLA"


# ──────────────────────────────────────────────────────────────────────────
# sad path — 잘못된 입력·실패 상황에서도 사이클이 크래시하지 않고 우아하게 넘어간다.
# (거짓 VIOLATION 도 내지 않는다 — 근거가 없으면 UNKNOWN)
# ──────────────────────────────────────────────────────────────────────────


def test_unsupported_operation_returns_error_string_not_crash():
    """지원 안 하는 operation → 예외가 아니라 에러 문자열(LLM 이 읽고 자가수정할 재료)."""
    principals = _principals()
    result = resolve_skeleton(
        "withdraw_all_money", "attacker_A", f"account:{VICTIM_ACCT}", principals, _graph(principals), load_target("vulnbank")
    )
    assert isinstance(result, str)
    assert "생성기가 없음" in result


def test_transfer_to_unknown_account_returns_error_string():
    """그래프가 모르는 계좌를 target 으로 지목 → 에러 문자열(크래시 X)."""
    principals = _principals()
    result = resolve_skeleton(
        "transfer", "attacker_A", "account:99999999", principals, _graph(principals), load_target("vulnbank")
    )
    assert isinstance(result, str)
    assert "모르는 계좌" in result


def test_read_balance_of_unknown_account_returns_error_string():
    principals = _principals()
    result = resolve_skeleton(
        "read_balance", "attacker_A", "account:99999999", principals, _graph(principals), load_target("vulnbank")
    )
    assert isinstance(result, str)
    assert "모르는 계좌" in result


def test_transfer_without_sender_entitlement_returns_error_string():
    """발신자가 자기 계좌에 entitled_own 이 없으면(그래프 비어있음) 에러 문자열."""
    principals = _principals()
    empty_graph = KnowledgeGraph()  # 아무 entitlement 도 없음
    result = resolve_skeleton(
        "transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, empty_graph, load_target("vulnbank")
    )
    assert isinstance(result, str)
    assert "entitled_own" in result


def test_missing_baseline_does_not_produce_false_violation():
    """baseline 없이 음수 송금 → INV-1B 는 확정 불가(UNKNOWN). 거짓 VIOLATION 이면 안 된다."""
    session = _mock_target_session()  # 부호반전 버그는 있지만
    principals = _principals()
    graph = _graph(principals)
    empty_baselines = BaselineRegistry()  # baseline 학습 안 함 → 판정 근거 없음

    skeleton = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session.target)
    assert not isinstance(skeleton, str)
    candidate = build_candidate(skeleton, {"amount": "-500.00", "to_account": VICTIM_ACCT}, "", empty_baselines)

    result = execute(session, session.target, candidate, principals[0], watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    finding = verify(candidate, result, graph)
    assert finding.verdict != Verdict.VIOLATION  # 근거 없이 위반이라 우기지 않는다


def test_server_error_does_not_crash_or_false_violation():
    """대상이 전부 500 을 뱉어도 → 크래시 없이, 거짓 VIOLATION 없이 넘어간다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session.target)
    assert not isinstance(skeleton, str)
    candidate = build_candidate(skeleton, {"amount": "-500.00", "to_account": VICTIM_ACCT}, "", baselines)

    result = execute(session, session.target, candidate, principals[0], watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    finding = verify(candidate, result, graph)  # 예외 없이 여기까지 온다
    assert finding.verdict != Verdict.VIOLATION


def test_connection_error_degrades_gracefully_not_crash():
    """대상 연결이 끊겨도(ConnectError) 크래시 없이 넘어간다.

    executor/burst 은 SessionError 만 잡으므로, 유일한 네트워크 지점인
    session._request 가 httpx 전송 에러(ConnectError/TimeoutException 등)를
    SessionError 로 변환해야 한다 — 안 그러면 실행 중 일시적 네트워크 장애로
    전체 run 이 죽는다.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    session = Session(load_target("vulnbank"), _policy(), _transport=httpx.MockTransport(handler))
    principals = _principals()
    graph = _graph(principals)
    baselines = _seed_baseline()

    skeleton = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session.target)
    assert not isinstance(skeleton, str)
    candidate = build_candidate(skeleton, {"amount": "-500.00", "to_account": VICTIM_ACCT}, "", baselines)

    result = execute(session, session.target, candidate, principals[0], watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    finding = verify(candidate, result, graph)  # 크래시 없이 여기까지
    assert finding.verdict != Verdict.VIOLATION
