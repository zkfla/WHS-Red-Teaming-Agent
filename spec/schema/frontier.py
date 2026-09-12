from __future__ import annotations

from pydantic import BaseModel

from spec.schema.attack_graph import AttackGraph, GraphView
from spec.schema.policy import PolicyPort, check_manifest
from spec.schema.registry import ToolRegistry


class FrontierEntry(BaseModel):
    tool_id: str
    reason: str = ""                      # 막혔거나 못 도는 이유 (사람이 읽는 문장)
    rule: str = ""                        # 정책 차단일 때의 규칙 id
    bindings: dict[str, list[str]] = {}   # 가용성 확인용 잠정 바인딩


class FrontierReport(BaseModel):
    step: int = 0
    ready: list[FrontierEntry] = []
    blocked: list[FrontierEntry] = []     # 전제는 맞는데 정책이 거부
    unmet: list[FrontierEntry] = []       # 전제 미충족

    def ready_ids(self) -> set[str]:
        return {e.tool_id for e in self.ready}

    def summary(self) -> str:
        return (f"실행가능 {len(self.ready)} · 정책차단 {len(self.blocked)} · "
                f"전제미충족 {len(self.unmet)}")


def compute_frontier(
    graph: AttackGraph | GraphView,
    registry: ToolRegistry,
    policy: PolicyPort,
    *,
    current_step: int = 0,
) -> FrontierReport:
    report = FrontierReport(step=current_step)

    for m in registry.all():
        if m.deprecated or not m.mcp.expose:
            continue

        # ★ 정책을 **먼저** 본다. 순서가 중요하다:
        #   정책 거부는 그래프 상태와 무관한 **영구적** 커버리지 공백이고,
        #   전제 미충족은 나중에 열릴 수도 있는 일시적 상태다.
        #   전제를 먼저 보면, 승인이 없어서 영영 못 하는 영역이 "아직 전제가
        #   안 맞아서" 뒤에 숨어버려 리포트가 커버리지를 속이게 된다.
        denials = check_manifest(policy, m)
        if denials:
            report.blocked.append(FrontierEntry(
                tool_id=m.id, rule=denials[0].rule,
                reason="; ".join(d.detail for d in denials),
            ))
            continue

        bindings = graph.resolve(
            m.preconditions, current_step=current_step, for_frontier=True
        )
        if isinstance(bindings, str):
            report.unmet.append(FrontierEntry(tool_id=m.id, reason=bindings))
            continue

        report.ready.append(FrontierEntry(tool_id=m.id, bindings=bindings))

    return report


def mcp_tool_list(registry: ToolRegistry, report: FrontierReport) -> list[dict]:
    return registry.mcp_tools(available=report.ready_ids())
