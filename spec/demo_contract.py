import json
from spec.schema.attack_graph import (
    AllowedSurfaces, AttackGraph, Node, Relation, Transition, OBJECTIVE_CATALOG,
)
from spec.schema.finance import EndpointFacts, MoneyValue, Provenance, ValueFlow, ValueLeg
from spec.schema.registry import ToolRegistry
from spec.schema.tool_manifest import ExecutionRecord, ProbeResult, RequestRecord

REG, _errs = ToolRegistry.load("spec/primitives")
assert not _errs, _errs
m = REG.require("http_request")
g = AttackGraph()

print("═" * 70); print(" 1. MCP 로 내보낼 때 — 뭐가 나가고 뭐가 남나"); print("═" * 70)
print(json.dumps(m.to_mcp_tool(), ensure_ascii=False, indent=2)[:430], "...")
print(f"\n  host 에만 남는 것 → capabilities={m.capabilities.required}  "
      f"surface_binding={m.surface_binding!r}  preconditions={len(m.preconditions)}개")
print(f"  노드여야 하는 파라미터: {m.node_bound_params()}")
print(f"  실행 직전 금액 재검사: {m.amount_params()}")
print(f"  기록에서 가려지는 파라미터: {sorted(m.sensitive_params())}")

print("\n" + "═" * 70); print(" 2. 그래프 준비 — 정찰로 발견한 표면만 칠 수 있다"); print("═" * 70)
P = lambda st, tool, ref, c="measured": Provenance(step=st, tool_id=tool, evidence_ref=ref, confidence=c)
g.upsert(Node(node_id="endpoint:POST /transfer", kind="endpoint", key="POST /transfer",
              attrs={"reachable": True}, source="tool:probe_path",
              facts=EndpointFacts(method="POST", path="/transfer", requires_auth=True,
                                  discovered_via="tool:probe_path")))
g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
              attrs={"authenticated": True, "has_account": True}))
g.add_relation(Relation(src="principal:attacker", dst="account:2344073575",
                        kind="entitled", relation="entitled_own",
                        provenance=P(1, "bootstrap", "ev-000")))
print(f"  발견된 표면: {[n for n in g.nodes if n.startswith('endpoint:')]}")
print(f"  전제 해소(가용성): {g.resolve(m.preconditions, for_frontier=True)}")
print(f"  전제 해소(확정)  : "
      f"{g.resolve(m.preconditions, params={'target': 'endpoint:POST /transfer'})}")

print("\n" + "═" * 70); print(" 3. 실행 결과 → 그래프 기록 (불변식 강제 지점)"); print("═" * 70)
usd = dict(currency="USD", minor_unit=2)
flow = ValueFlow(transition_id="t-07", closed=True, legs=[
    ValueLeg(node_id="account:victim",   delta=MoneyValue(amount="-500.00", **usd), provenance=P(7,"http_request","ev-101")),
    ValueLeg(node_id="account:attacker", delta=MoneyValue(amount="500.00",  **usd), provenance=P(7,"http_request","ev-102")),
])
res = ProbeResult(tool_id="http_request", executions=[ExecutionRecord(
    label="음수 송금", requests=[RequestRecord(method="POST", path="/transfer",
    status_code=200, evidence_ref="ev-101", response_field_names=["status","new_balance"])],
    value_flow=flow)])
print(f"  만진 표면: {res.touched_surfaces()}   증거: {res.evidence_refs()}")

for label, t in [
    ("정상", Transition(transition_id="t-07", step=7, tool_id="http_request", tool_version=1,  # noqa: E501
                        status="succeeded", value_flow=flow, finding_ids=["f-07"],
                        evidence_refs=res.evidence_refs(), touched_surfaces=res.touched_surfaces())),
    ("증거 없이 주장", Transition(transition_id="t-08", step=8, tool_id="http_request", tool_version=1,
                        status="succeeded", finding_ids=["f-08"])),
    ("선언 밖 표면", Transition(transition_id="t-09", step=9, tool_id="http_request", tool_version=1,
                        status="succeeded", touched_surfaces=["POST /transfer", "GET /sup3r_s3cr3t_admin"])),
]:
    probs = g.record_transition(t, manifest_surface=AllowedSurfaces(keys={"POST /transfer"}))
    print(f"  [{label}] → " + ("통과" if not probs else f"★ {probs[0]}"))

print("\n" + "═" * 70); print(" 4. 리포트 직전 점검 + 목표 카탈로그"); print("═" * 70)
for k, v in g.integrity_report().items():
    print(f"  {k:12s}: {v or '없음'}")
print(f"\n  보존 깨진 전이: {[t.transition_id for t in g.unconserved_flows()] or '없음'}"
      f"   (t-07 은 -500/+500 → 보존됨, 방향만 위반)")
print(f"  목표 카탈로그 {len(OBJECTIVE_CATALOG)}개 — 매 run 전부 평가, 줄일 수 없음:")
for o in OBJECTIVE_CATALOG:
    print(f"    {o.objective:20s} {o.severity:8s} {o.requires_any_invariant}")

print(f"\n  저장 레코드 {len(g.to_jsonl_records())}줄 "
      f"(컬렉션: {sorted({r['_collection'] for r in g.to_jsonl_records()})})")
