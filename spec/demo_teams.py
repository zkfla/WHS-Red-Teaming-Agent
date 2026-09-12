from spec.schema.attack_graph import (
    AttackGraph, FindingRecord, GraphView, Node, Relation, RunContext, Transition,
    endpoint_id,
)
from spec.schema.finance import (
    EndpointFacts, MoneyValue, Provenance, ValueFlow, ValueLeg,
)
from spec.schema.frontier import compute_frontier, mcp_tool_list
from spec.schema.ingest import ingest, preflight
from spec.schema.policy import StaticPolicy
from spec.schema.registry import ToolRegistry
from spec.schema.tool_manifest import ExecutionRecord, ProbeResult, RequestRecord

REG, errs = ToolRegistry.load("spec/primitives")
assert not errs, errs
m = REG.require("http_request")
P = lambda st, ref, c="measured": Provenance(step=st, tool_id="http_request",
                                             evidence_ref=ref, confidence=c)

POLICY = StaticPolicy(
    engagement_id="eng-vulnbank", allow_state_change=True, allow_account_creation=True,
    max_concurrency=1, max_amount=MoneyValue(amount="1000.00", currency="USD"),
    max_requests_per_step=100, max_duration_s=120.0, allowed_path_prefixes=["/"],
)

# ④ Control Plane 이 그래프를 만들고 소유한다. policy_hash 가 승인 범위를 증명한다.
g = AttackGraph(run=RunContext(run_id="r-001", engagement_id=POLICY.engagement_id,
                               policy_hash=POLICY.policy_hash(),
                               target_name="vulnbank", max_steps=20))
for method, path in (("POST", "/transfer"), ("GET", "/check_balance/{id}")):
    nid = endpoint_id(method, path)
    g.upsert(Node(node_id=nid, kind="endpoint", key=nid.split(":", 1)[1],
                  attrs={"reachable": True}, first_seen_step=1, source="tool:probe_path",
                  facts=EndpointFacts(method=method, path=path, requires_auth=True,
                                      discovered_via="tool:probe_path")))
g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
              attrs={"authenticated": True, "has_account": True, "ours": True},
              first_seen_step=1))
g.add_relation(Relation(src="principal:attacker", dst="account:attacker", kind="entitled",
                        relation="entitled_own", provenance=P(1, "ev-000")))

print("═" * 74); print(" ① Agent / ③ Capability — 읽기 전용 뷰만 받는다"); print("═" * 74)
view = GraphView(g)
print(f"  관측 요약: {view.observe(current_step=2)['nodes']}")
print(f"  공격자 계좌(그래프에서 유도): {view.attacker_accounts()}")
try:
    view.upsert(Node(node_id="card:99", kind="card", key="99"))
except AttributeError as e:
    print(f"  직접 쓰기 차단 → {str(e).split('—')[1].strip()[:58]}")

print("\n" + "═" * 74); print(" ② Orchestration — frontier 계산 (한 스텝의 ①번)"); print("═" * 74)
r = compute_frontier(g, REG, POLICY, current_step=2)
print(f"  {r.summary()}")
print(f"  모델에게 보낼 tool: {sorted(t['name'] for t in mcp_tool_list(REG, r))}")

print("\n" + "═" * 74); print(" ④ Control Plane — 실행 전 검사 (그래프 + 정책)"); print("═" * 74)
GOOD = {"target": "endpoint:POST /transfer", "method": "POST",
        "auth_as": "principal:attacker", "amount": "10.00"}
for label, params in [
    ("정상",               GOOD),
    ("발견 안 한 표면",     {"target": "endpoint:GET /sup3r_s3cr3t_admin", "method": "GET"}),
    ("금액에 float",        {"target": "endpoint:POST /transfer", "method": "POST", "amount": 10.5}),
    ("정책 금액 초과",      {"target": "endpoint:POST /transfer", "method": "POST", "amount": "99999.00"}),
    ("선언 안 된 파라미터",  {"target": "endpoint:POST /transfer", "sql": "' OR 1=1"}),
    ("바디가 깨진 JSON",    {"target": "endpoint:POST /transfer", "body": "{ 깨짐"}),
]:
    b, probs = preflight(g, m, params, current_step=2, policy=POLICY)
    print(f"  [{label:16s}] " + ("통과" if not probs else f"★ {probs[0][:64]}"))

bindings, _ = preflight(g, m, GOOD, current_step=2, policy=POLICY)
print(f"\n  ★ 확정된 바인딩: {bindings}")
print(f"    모델이 고른 {GOOD['target']!r} 과 같은가 → "
      f"{bindings['target'] == [GOOD['target']]}")
print("    (미끼로 'GET /check_balance/{id}' 도 전제를 만족하지만 집히지 않았다)")

print("\n" + "═" * 74); print(" ③ Capability → ② Graph — 결과 흡수 (유일한 경로)"); print("═" * 74)
usd = dict(currency="USD", minor_unit=2)
flow = ValueFlow(transition_id="t-03", closed=True, legs=[
    ValueLeg(node_id="account:victim",   delta=MoneyValue(amount="-500.00", **usd), provenance=P(3, "ev-101")),
    ValueLeg(node_id="account:attacker", delta=MoneyValue(amount="500.00",  **usd), provenance=P(3, "ev-102")),
])
res = ProbeResult(tool_id="http_request", tool_version=1, executions=[ExecutionRecord(
    label="음수 송금", value_flow=flow,
    requests=[RequestRecord(method="POST", path="/transfer", status_code=200,
                            evidence_ref="ev-101", response_field_names=["status", "new_balance"])])])
t = Transition(transition_id="t-03", step=3, tool_id="http_request", tool_version=1,
               status="succeeded", value_flow=flow,
               params=dict(GOOD), bindings=bindings)          # ★ preflight 의 바인딩을 그대로
rep = ingest(g, m, res, t, findings=[FindingRecord(
    finding_id="f-01", invariant="INV-1B", verdict="VIOLATION", transition_id="",
    reason="송금 -500.00 요청에 발신 +500.00 / 수신 -500.00 관측 — 부호 반전",
    evidence_refs=["ev-101"], severity="HIGH")])
print(f"  {rep}")
print(f"  전이에 붙은 증거: {t.evidence_refs}   만진 표면: {t.touched_surfaces}")
print(f"  이탈: {t.surface_drift or '없음'}   기록된 params: {t.params}")

print("\n  [이상 케이스] 선언 밖 표면 + 선언 밖 노드 + 미선언 실패코드 + 버전 불일치")
bad = ProbeResult(tool_id="http_request", tool_version=2, failure_code="mystery",
                  produced_nodes=[{"node_id": "role:admin", "kind": "role", "key": "admin"}],
                  executions=[ExecutionRecord(requests=[RequestRecord(
                      method="GET", path="/sup3r_s3cr3t_admin", status_code=200, evidence_ref="ev-9")])])
t_bad = Transition(transition_id="t-04", step=4, tool_id="http_request", tool_version=1,
                   status="succeeded", bindings=bindings)
for pr in ingest(g, m, bad, t_bad).problems:
    print(f"    ★ {pr[:92]}")

print("\n" + "═" * 74); print(" ④ Evidence — 저장하고 다시 읽는다"); print("═" * 74)
g.evaluate_objectives()                     # ★ 공격자 계좌를 손으로 안 넘긴다
recs = g.to_jsonl_records()
back = AttackGraph.from_jsonl_records(recs)
print(f"  저장 {len(recs)}줄 → 로드 {len(back.nodes)}노드 {len(back.transitions)}전이 "
      f"{len(back.findings)}판정   왕복 일치: {g.round_trip_ok()}")
for o in g.objectives:
    if o.verdict != "UNKNOWN":
        print(f"  {o.verdict:9s} {o.objective:20s} [{o.severity}] {o.reason[:52]}")
print(f"\n  무결성: {({k: v for k, v in g.integrity_report().items() if v}) or '문제 없음'}")
print("  → 비어 있지 않으면 리포트를 내면 안 된다. 여기서는 t-04 의 이탈이 잡혀 있다.")
