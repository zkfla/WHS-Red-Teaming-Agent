from __future__ import annotations

from pydantic import BaseModel

from spec.schema.attack_graph import AttackGraph, GraphView, Transition
from spec.schema.frontier import compute_frontier, mcp_tool_list
from spec.schema.ingest import ingest, preflight
from spec.schema.policy import PolicyPort
from spec.schema.ports import AgentPort, Executor, ExecutionRequest, OraclePort
from spec.schema.registry import ToolRegistry

# ══════════════════════════════════════════════════════════════════════
#  한 스텝의 **정본 배선**. ②가 자기 루프를 직접 짜도 되지만, 그때도 이 순서를
#  그대로 따라야 한다.
#
#  ★ 이 파일을 두는 이유는 순서가 아니라 **값의 전달**이다.
#    preflight() 가 만든 bindings 를 ③ 실행과 ⑤ ingest 에 **똑같이** 넘겨야 하는데,
#    이 인계는 타입으로 강제할 수가 없다(양쪽 다 dict 를 받으니까).
#    실제로 이 인계가 끊긴 채로 테스트 27개가 전부 통과하고 있었다 —
#    데모와 테스트가 바인딩을 손으로 써 넣고 있었기 때문이다.
#    그래서 코드로 한 번 박아둔다.
# ══════════════════════════════════════════════════════════════════════


class StepOutcome(BaseModel):
    step: int
    stopped: bool = False          # ① 이 "더 할 게 없다" 고 했다
    tool_id: str = ""
    transition_id: str = ""
    status: str = ""               # Transition.status 와 같은 값
    problems: list[str] = []

    def ok(self) -> bool:
        return not self.problems and not self.stopped


def _rule_of(problems: list[str]) -> str:
    # 정책 거부 문자열에서 규칙 id 만 뽑는다: "tool: [AMOUNT_OVER_LIMIT] ..." → 그 대괄호 안
    for p in problems:
        if "[" in p and "]" in p:
            return p[p.index("[") + 1 : p.index("]")]
    return "PREFLIGHT_REJECTED"


def run_step(
    *,
    step: int,
    graph: AttackGraph,
    registry: ToolRegistry,
    policy: PolicyPort,
    agent: AgentPort,
    executor: Executor,
    oracle: OraclePort | None = None,
) -> StepOutcome:
    view = GraphView(graph)
    tid = f"t-{step:04d}"

    # ① frontier — 정책이 영구 차단한 것과 전제가 아직 안 맞는 것을 나눠서 본다
    report = compute_frontier(graph, registry, policy, current_step=step)

    # ② 모델이 고른다. 여기가 유일한 LLM 개입 지점이다.
    call = agent.choose(
        view.observe(current_step=step), mcp_tool_list(registry, report), step=step
    )
    if call is None:
        return StepOutcome(step=step, stopped=True)

    manifest = registry.get(call.tool_id)
    if manifest is None:
        return StepOutcome(step=step, tool_id=call.tool_id,
                           problems=[f"레지스트리에 없는 tool {call.tool_id!r}"])

    # ★ frontier 밖의 tool 을 고르면 거부한다. 목록에서 숨겼는데도 골랐다는 건
    #   ① 이 목록 밖을 짐작했다는 뜻이라, 그 자체로 기록에 남아야 한다.
    if call.tool_id not in report.ready_ids():
        blocked = next((e for e in report.blocked if e.tool_id == call.tool_id), None)
        reason = blocked.rule if blocked else "NOT_IN_FRONTIER"
        t = Transition(
            transition_id=tid, step=step, tool_id=manifest.id,
            tool_version=manifest.version, status="blocked", blocked_reason=reason,
            agent_id=call.agent_id, params=manifest.redact(call.params),
        )
        graph.record_transition(t, manifest_surface=manifest.allowed_surfaces())
        return StepOutcome(step=step, tool_id=call.tool_id, transition_id=tid,
                           status="blocked", problems=[f"frontier 밖 tool — {reason}"])

    # ③ preflight — 파라미터·노드 실존·정책·전제. 여기서 bindings 가 **확정**된다.
    bindings, problems = preflight(
        graph, manifest, call.params, current_step=step, policy=policy
    )
    if problems or bindings is None:
        t = Transition(
            transition_id=tid, step=step, tool_id=manifest.id,
            tool_version=manifest.version, status="blocked",
            blocked_reason=_rule_of(problems),
            agent_id=call.agent_id, params=manifest.redact(call.params),
            requires=list(manifest.preconditions),
        )
        graph.record_transition(t, manifest_surface=manifest.allowed_surfaces())
        return StepOutcome(step=step, tool_id=call.tool_id, transition_id=tid,
                           status="blocked", problems=problems)

    # ④ 실행 — ③ Capability. 확정된 bindings 를 그대로 넘긴다.
    if not executor.supports(manifest.id):
        return StepOutcome(step=step, tool_id=call.tool_id,
                           problems=[f"executor 가 {manifest.id!r} 를 지원하지 않는다"])
    result = executor.execute(
        manifest,
        ExecutionRequest(tool_id=manifest.id, step=step,
                         params=call.params, bindings=bindings),
    )

    # ⑤ ingest — **같은** bindings 를 전이에 실어 보낸다. 이게 스코프 대조의 기준이 된다.
    t = Transition(
        transition_id=tid, step=step, tool_id=manifest.id,
        tool_version=manifest.version,
        status="failed" if result.error else "succeeded",
        agent_id=call.agent_id, params=dict(call.params), bindings=bindings,
        requires=list(manifest.preconditions),
    )
    rep = ingest(graph, manifest, result, t, oracle=oracle)

    # ⑥ 목표 카탈로그 전체 평가 — 매 스텝 돈다. 줄일 수 없다.
    graph.evaluate_objectives()

    return StepOutcome(step=step, tool_id=manifest.id, transition_id=tid,
                       status=t.status, problems=rep.problems)
