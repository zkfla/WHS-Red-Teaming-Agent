from spec.schema.attack_graph import AttackGraph, Node
from spec.schema.registry import ToolRegistry

REG, _errs = ToolRegistry.load("spec/primitives")
assert not _errs, _errs
TOOLS = REG.as_dict()


def ready(g: AttackGraph) -> list[str]:
    # ★ for_frontier=True — 이 시점에는 모델이 아직 params 를 안 골랐다.
    #   "후보가 있는가" 만 보고, 어느 노드를 쓸지는 preflight 가 확정한다.
    return sorted(t.id for t in TOOLS.values()
                  if isinstance(g.resolve(t.preconditions, for_frontier=True), dict))


def apply(g: AttackGraph, tool_id: str, step: int, key: str) -> None:
    for pc in TOOLS[tool_id].postconditions:
        g.upsert(Node(node_id=f"{pc.kind}:{key}", kind=pc.kind, key=key,
                      attrs=dict(pc.attrs), source=f"tool:{tool_id}", first_seen_step=step))


g = AttackGraph()
print("시작: 빈 그래프 — 아는 게 하나도 없다\n")

CHAIN = [
    ("probe_path",       "POST /login",  "로그인 경로를 발견한다"),
    ("create_principal", "u1",           "계정을 만든다"),
    ("authenticate",     "u1",           "로그인해 토큰을 얻는다"),
    ("forge_token",      "u1",           "그 토큰 구조를 베껴 admin 클레임으로 위조한다"),
]

for step, (tool, key, why) in enumerate(CHAIN, 1):
    before = ready(g)
    if tool not in before:
        print(f"[{step}] {tool} — 아직 실행 불가: "
              f"{g.resolve(TOOLS[tool].preconditions, for_frontier=True)}")
        break
    apply(g, tool, step, key)
    opened = [t for t in ready(g) if t not in before]
    print(f"[{step}] {tool:18s} {why}")
    print(f"    실행가능 {len(before)}개" + (f"  →  새로 열림: {', '.join(opened)}" if opened else ""))

print("\n최종 그래프:")
for n in sorted(g.nodes.values(), key=lambda x: x.first_seen_step):
    print(f"  {n.node_id:22s} {n.attrs}   ← {n.source}")

print("""
★ 누가 이 순서를 적었나? 아무도 안 적었다.
  probe_path 가 endpoint 를 만들어야 create_principal 이 돌고,
  authenticate 가 token 을 만들어야 forge_token 이 돈다.
  각 tool 은 옆 tool 의 존재를 모른다 — 순서는 그래프가 만들었다.

★ 이게 레시피와 다른 점이다. "로그인 경로 찾고 → 가입하고 → 로그인하고 → 위조해라"
  를 사람이 적어두면 그건 절차서다. 전제와 결과만 적으면 **사람이 안 본 순서도** 나온다.""")
