"""Correlated display projection. No runtime or policy decisions live here."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from rich.markdown import Markdown
from rich.text import Text

from redteam.app.cli.display import safe_text
from redteam.core.contracts.events import EventType


@dataclass
class ActionView:
    action_id: str
    operation: str = "action"
    target: str = "대상 정보 없음"
    policy: str = ""
    tool: str = ""
    result: str = ""
    finished: bool = False
    changes: list[str] = field(default_factory=list)


class SessionProjection:
    def __init__(self, controller, *, verbose: bool = False) -> None:
        self.controller = controller
        self.verbose = verbose
        self.actions: OrderedDict[tuple[str, str], ActionView] = OrderedDict()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.findings: set[str] = set()
        self.finding_events = 0
        self.policy_blocks = 0
        self.closed = False

    async def __call__(self, event) -> None:
        c = self.controller
        event_id = str(event.event_id)
        if event_id in self.seen:
            return
        self.seen[event_id] = None
        if len(self.seen) > 2048:
            self.seen.popitem(last=False)
        kind, p = event.event_type, event.payload
        detail = safe_text(f"{event.timestamp.isoformat()} · Run {event.run_id} · {kind} · {p}")
        c.output.history.append(detail)
        if self.closed:
            if "Failed" in kind or kind == EventType.ACTION_DENIED:
                c.output.emit(f"[이전 Run {event.run_id}] {detail}")
            return
        action_id = p.get("action_id")
        key = (str(event.run_id), str(action_id)) if action_id else None
        action = self.actions.get(key) if key else None
        if key and action is None:
            action = ActionView(str(action_id))
            self.actions[key] = action
            if len(self.actions) > 256:
                self.actions.popitem(last=False)
        if kind == "AgentTurnDecided":
            response = p.get("response") or p.get("decision_summary")
            if response:
                c.output.emit(Markdown(safe_text(response), hyperlinks=False))
            reason = p.get("decision_summary")
            if reason and reason not in str(response):
                c.output.emit(Text("판단 요약: " + safe_text(reason), style="dim"))
        elif kind == EventType.ACTION_PROPOSED:
            label = safe_text(
                f"{p.get('operation', 'action')} · {p.get('target', '대상 정보 없음')}"
            )
            if action:
                action.operation = safe_text(p.get("operation", "action"))
                action.target = safe_text(p.get("target", "대상 정보 없음"))
            c.activity = "진행 중 · " + label
        elif kind == "MCPToolSelected":
            tool = safe_text(
                f"{p.get('server_id', 'MCP')}/{p.get('tool_name', p.get('tool_id', '?'))}"
            )
            if action:
                action.tool = tool
            c.activity = f"MCP 선택 · {tool} · health={p.get('health_status', '확인 불가')}"
        elif kind == EventType.POLICY_EVALUATED:
            verdict = str(p.get("verdict", "unknown")).upper()
            reason = safe_text("; ".join(map(str, p.get("reasons", []))))
            policy = safe_text(f"{verdict} · {reason} · policy={p.get('matched_rules', [])}")
            if action:
                action.policy = policy
            if verdict != "ALLOW":
                c.output.emit(Text(f"! [Control] {policy}", style="yellow"))
        elif kind in {EventType.ACTION_DENIED, EventType.APPROVAL_REQUIRED}:
            if kind == EventType.ACTION_DENIED:
                self.policy_blocks += 1
            label = "정책 차단" if kind == EventType.ACTION_DENIED else "실행 승인 필요"
            context = (
                f"{action.operation} · {action.target}\n{action.policy}" if action else safe_text(p)
            )
            # A denial/approval can arrive without the proposal or policy event.
            # Its own reasons remain authoritative and must not disappear.
            if action and any(k in p for k in ("reasons", "reason", "matched_rules")):
                context += "\n" + safe_text(
                    {k: p[k] for k in ("reasons", "reason", "matched_rules") if k in p}
                )
            approval = p.get("approval_id")
            text = f"! {label} · Run {event.run_id}\n  {context}"
            if approval:
                text += f"\n  approve {approval} / deny {approval} · 이 요청의 action만"
            c.output.emit(Text(safe_text(text), style="yellow"))
            c.activity = safe_text(text)
        elif kind in {EventType.TOOL_EXECUTION_FINISHED, EventType.TOOL_EXECUTION_FAILED}:
            success = kind == EventType.TOOL_EXECUTION_FINISHED and p.get("success", False)
            result = p.get("summary") or p.get("error_message") or p.get("error_code")
            result = safe_text(
                result or ("도구 호출 성공 — 상세는 /trace" if success else "도구 호출 실패")
            )
            title = (
                f"{action.operation} · {action.target}" if action else "도구 결과 (연결 정보 없음)"
            )
            lines = [f"{'✓' if success else '✗'} {title}"]
            if action:
                action.result, action.finished = result, True
                lines.extend(item for item in (action.policy, action.tool) if item)
            lines.append(result)
            c.output.emit(
                Text(safe_text("\n  ".join(lines)), style="default" if success else "red")
            )
            c.activity = "후속 결과/판단 대기"
        elif kind in {EventType.GRAPH_UPDATED, "GraphMutationApplied"}:
            summary = safe_text(f"[State] {p}", limit=400)
            if action:
                action.changes.append(summary)
            c.output.emit(summary, critical=False)
        elif kind in {EventType.FINDING_CREATED, EventType.FINDING_VERIFIED}:
            if p.get("finding_id"):
                self.findings.add(str(p["finding_id"]))
            else:
                self.finding_events += 1
            c.output.emit(f"! Finding · {safe_text(p.get('title') or p.get('summary') or p)}")
        elif "Failed" in kind or kind in {"ReplanTriggered", "AuthenticationCheckDeferred"}:
            c.output.emit(f"[{kind}] {safe_text(p)}")
        elif self.verbose and kind != EventType.RUN_FINISHED:
            c.output.emit(detail, critical=False)
        c.frontend.invalidate()
