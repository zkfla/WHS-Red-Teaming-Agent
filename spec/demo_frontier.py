from spec.schema.attack_graph import AttackGraph, Node, endpoint_id
from spec.schema.finance import MoneyValue
from spec.schema.frontier import compute_frontier, mcp_tool_list
from spec.schema.policy import StaticPolicy
from spec.schema.registry import ToolRegistry

REGISTRY, errs = ToolRegistry.load("spec/tools", "spec/primitives")
assert not errs, errs

FULL = StaticPolicy(
    engagement_id="eng-vulnbank-full",
    allow_state_change=True, allow_account_creation=True, allow_race_testing=True,
    allow_bruteforce=True, allow_file_upload=True, allow_delete=True,
    max_concurrency=10, max_amount=MoneyValue(amount="1000.00", currency="USD"),
    max_requests_per_step=100, max_duration_s=120.0,
)
TIGHT = StaticPolicy(
    engagement_id="eng-vulnbank-tight",
    allow_state_change=True, allow_account_creation=True,      # race/bruteforce 미승인
    max_concurrency=1, max_amount=MoneyValue(amount="1000.00", currency="USD"),
    max_requests_per_step=100, max_duration_s=120.0,
)


def show(title: str, g: AttackGraph, pol: StaticPolicy) -> None:
    r = compute_frontier(g, REGISTRY, pol)
    print(f"\n{'='*70}\n{title}\n{'='*70}")
    print(f"  정책 해시 {pol.policy_hash()}  ·  {r.summary()}")
    print(f"✅ 실행가능: {', '.join(sorted(r.ready_ids())) or '없음'}")
    if r.blocked:
        print(f"🚫 정책차단 — **영구** 커버리지 공백 (리포트에 정직하게 적어야 한다):")
        for e in sorted(r.blocked, key=lambda x: x.tool_id)[:6]:
            print(f"     {e.tool_id:22s} [{e.rule}] {e.reason[:44]}")
    if r.unmet:
        print(f"⏳ 전제미충족 — 나중에 열릴 수 있다:")
        for e in sorted(r.unmet, key=lambda x: x.tool_id)[:5]:
            print(f"     {e.tool_id:22s} {e.reason[:52]}")


g = AttackGraph()
show("[1] run 시작 — 아무것도 없음 (허용적 engagement)", g, FULL)

for method, path in (("POST", "/login"), ("POST", "/register"), ("POST", "/transfer")):
    nid = endpoint_id(method, path)
    g.upsert(Node(node_id=nid, kind="endpoint", key=nid.split(":", 1)[1],
                  attrs={"reachable": True}, source="tool:probe_path"))
show("[2] 정찰로 표면 3개 발견 후", g, FULL)

g.upsert(Node(node_id="principal:attacker", kind="principal", key="attacker",
              attrs={"authenticated": True, "has_account": True, "has_token": True,
                     "ours": True}))
show("[3] 계정 1개 부트스트랩 후", g, FULL)

g.upsert(Node(node_id="principal:victim", kind="principal", key="victim",
              attrs={"authenticated": True, "has_account": True}))
show("[4] 계정 2개 (race 계열 전제 충족)", g, FULL)

show("[5] ★ 같은 그래프 + 보수적 engagement (race/bruteforce 미승인)", g, TIGHT)

r = compute_frontier(g, REGISTRY, TIGHT)
names = [t["name"] for t in mcp_tool_list(REGISTRY, r)]
print(f"""
★ 모델에게 실제로 보내는 MCP tool 목록 ({len(names)}개): {', '.join(sorted(names))}

  · 정책이 막은 tool 은 목록에 없다 — 모델이 "이건 그 권한 필요 없는데요" 라고
    우길 대상 자체가 없다.
  · 정리 전용 tool(delete_*)도 없다 — 정리는 control plane 이 부른다.
  · 그래도 차단 **기록**은 남는다. 안 남기면 리포트가 커버리지를 속이게 된다.""")
