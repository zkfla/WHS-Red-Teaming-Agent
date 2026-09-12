from decimal import Decimal
from spec.schema.attack_graph import AllowedSurfaces, AttackGraph, Node, Transition
from spec.schema.finance import (
    AccountFacts, CardFacts, MoneyObservation, MoneyValue, PolicyBound,
    Provenance, ValueFlow, ValueLeg,
)

USD = dict(currency="USD", minor_unit=2)
def M(a): return MoneyValue(amount=a, **USD)                 # 문자열로만 — float 불가
def P(step, tool, ref, conf="measured"):
    return Provenance(step=step, tool_id=tool, evidence_ref=ref, confidence=conf)

g = AttackGraph()

# ── 1. 실험 전 관측 ─────────────────────────────────────────────────
acct = AccountFacts(account_number="2344073575", currency="USD",
                    owner_principal="principal:attacker")
acct.balance_history.append(MoneyObservation(value=M("900.00"), provenance=P(3,"observe_state","ev-001")))
card = CardFacts(card_id="17", currency="USD", owner_principal="principal:attacker",
                 status="active", pan_last4="4242")
card.balance_history.append(MoneyObservation(value=M("0.00"), provenance=P(3,"observe_state","ev-002")))
card.declared_limit = MoneyObservation(value=M("9999999.00"), provenance=P(3,"http_request","ev-003"))

g.upsert(Node(node_id="account:2344073575", kind="account", key="2344073575",
              attrs={"balance_known": True}, facts=acct, source="tool:observe_state"))
g.upsert(Node(node_id="card:17", kind="card", key="17",
              attrs={"created": True}, facts=card, source="tool:http_request"))

print("── 실험 전 ──")
print(f"  계좌 잔액: {acct.latest_balance().value}   (출처 {acct.latest_balance().provenance.evidence_ref})")
print(f"  카드 잔액: {card.latest_balance().value}")
print(f"  카드 한도: {card.declared_limit.value}  ·  PAN 뒷4자리 {card.pan_last4}  CVV노출 {card.cvv_exposed}")

# ── 2. 공격: 100 USD 충전, exchange_rate=100 주입 ───────────────────
acct.balance_history.append(MoneyObservation(value=M("800.00"), provenance=P(4,"http_request","ev-010")))
card.balance_history.append(MoneyObservation(value=M("10000.00"), provenance=P(4,"http_request","ev-011")))

print("\n── 실험 후 (exchange_rate=100 주입) ──")
print(f"  계좌 {acct.balance_at(3).value} → {acct.latest_balance().value}   델타 {acct.delta(3,4)}")
print(f"  카드 {card.balance_at(3).value} → {card.latest_balance().value}   델타 {card.delta(3,4)}")

# ── 3. 가치 이동 기록 → 판정 재료 ───────────────────────────────────
flow = ValueFlow(transition_id="t-04", closed=True, legs=[
    ValueLeg(node_id="account:2344073575", delta=acct.delta(3,4), provenance=P(4,"http_request","ev-010")),
    ValueLeg(node_id="card:17",            delta=card.delta(3,4), provenance=P(4,"http_request","ev-011")),
])
t = Transition(transition_id="t-04", step=4, tool_id="http_request", tool_version=1,
               status="succeeded", value_flow=flow,
               finding_ids=["f-01"], evidence_refs=["ev-010","ev-011"],
               touched_surfaces=["POST /api/virtual-cards/{id}/fund"])
# ★ transitions 에 직접 append 하지 않는다 — 불변식 강제 지점을 통과해야 한다
g.record_transition(t, manifest_surface=AllowedSurfaces(
    keys={"POST /api/virtual-cards/{id}/fund"}))

print("\n── 가치 이동 판정 재료 ──")
print(f"  통화별 순합: {{k: str(v) for k,v in flow.net().items()}}".replace("{k: str(v) for k,v in flow.net().items()}",
      str({k: str(v) for k, v in flow.net().items()})))
print(f"  이득 본 쪽: {[l.node_id for l in flow.beneficiaries()]}")
print(f"  값이 나온 쪽: {[l.node_id for l in flow.sources()]}")
print(f"  → {flow.explain()}")
print(f"  보존 깨진 전이: {[x.transition_id for x in g.unconserved_flows()]}")

# ── 4. 정책 경계 — 증거 강도 구분 ───────────────────────────────────
print("\n── 정책 경계: 누가 선언했나에 따라 증거 강도가 다르다 ──")
server_bound = PolicyBound(field="amount", minimum=M("5.00"), declared_by="server",
                           provenance=P(5,"http_request","ev-020","server_declared"))
our_bound    = PolicyBound(field="loan_amount", maximum=M("1000000.00"), declared_by="probe_assertion",
                           provenance=P(6,"http_request","ev-021","asserted"))
for b, attempt in [(server_bound, M("-100.00")), (our_bound, M("100000000.00"))]:
    print(f"  [{b.evidence_strength:6s}] {b.declared_by:16s} → {b.violates(attempt)}")

# ── 5. 안전장치 확인 ────────────────────────────────────────────────
print("\n── 스키마가 구조적으로 막는 것 ──")
for label, fn in [
    ("float 금액",        lambda: MoneyValue(amount=100.5, **USD)),
    ("증거 없는 관측",     lambda: Provenance(step=1, tool_id="x", evidence_ref="")),
    ("전체 PAN 저장",     lambda: CardFacts(card_id="9", currency="USD", pan_last4="4242424242424242")),
]:
    try:
        fn(); print(f"  ✗ {label} — 통과됨(문제!)")
    except Exception as e:
        msg = next((l.strip() for l in str(e).splitlines()
                    if "Value error" in l or "Type error" in l or "쓸 수 없다" in l), "")
        msg = msg.split(", ", 1)[-1] if msg.startswith(("Value error", "Type error")) else msg
        print(f"  ✓ {label} 차단 — {msg or str(e).splitlines()[0]}")
