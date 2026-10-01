"""Session-wide input ownership, run affinity and bounded task lifetimes."""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict, deque
from dataclasses import replace
from uuid import uuid4

from rich.markdown import Markdown
from rich.text import Text

from redteam.app.cli.display import safe_text
from redteam.app.cli.input_router import CONTROL_COMMANDS, InputRouter, command
from redteam.app.cli.output_coordinator import OutputCoordinator
from redteam.app.cli.session_types import (
    BindingRef,
    InputEnvelope,
    InputReceipt,
    RunCompletionView,
    RunResult,
    SessionPhase,
)

INPUT_CAPACITY = 64
CONTROL_CAPACITY = 16
RECEIPT_CAPACITY = 512
DISPATCH_TIMEOUT = 10.0
CLOSE_TIMEOUT = 10.0
_UNSET = object()


class SessionController:
    def __init__(
        self,
        *,
        console,
        frontend,
        conversation,
        runner,
        engagement_name: str,
        scope: list[str],
        on_finished=None,
        slash_handler=None,
        resume_runner=None,
        context_loader=None,
        approval_verifier=None,
        commands=(),
        command_help=None,
        model_label: str = "",
        single_run: bool = False,
    ) -> None:
        self.frontend = frontend
        self.router = InputRouter(conversation)
        self.runner = runner
        self.engagement_name = engagement_name
        self.model_label = model_label
        self.command_help = {
            **(command_help or {}),
            "new": "새 조사 요청 (현재 scope 내)",
            "stop": "현재 Run 중단 요청",
            "pause": "현재 Run 일시 중단",
            "resume": "같은 Run 재개",
            "approve": "현재 승인 ID 확인 후 승인",
            "deny": "현재 승인 요청 거부",
            "trace": "최근 안전한 상세 기록",
            "pending": "보류·미확정 입력 조회",
            "cancel": "미전달 input ID 취소",
            "exit": "세션 종료 및 정리",
        }
        self.scope = scope
        self.on_finished = on_finished
        self.slash_handler = slash_handler
        self.resume_runner = resume_runner
        self.context_loader = context_loader
        self.approval_verifier = approval_verifier
        self.active_receipt = None
        self.commands = sorted(
            set(commands)
            | {
                "status",
                "explain",
                "stop",
                "pause",
                "resume",
                "approve",
                "deny",
                "exit",
                "help",
                "trace",
                "pending",
                "cancel",
                "new",
            }
        )
        self.single_run = single_run
        self.output = OutputCoordinator(console, frontend)
        self.phase = SessionPhase.IDLE
        self.binding: BindingRef | None = None
        self.last_binding: BindingRef | None = None
        self.pending_approval: str | None = None
        self.activity = "자연어로 질문하거나 조사를 요청하세요."
        self.receipts: OrderedDict[str, InputReceipt] = OrderedDict()
        self.snapshots: OrderedDict[str, str] = OrderedDict()
        self.recent_events: deque[str] = deque(maxlen=8)
        self.terminal_event_reason: str | None = None
        self.queue: asyncio.Queue = asyncio.Queue(INPUT_CAPACITY)
        self.controls: asyncio.Queue = asyncio.Queue(CONTROL_CAPACITY)
        self.deferred: deque[InputReceipt] = deque()
        self.gate = asyncio.Lock()
        self.closing = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.ready = asyncio.Event()
        self.operator = None
        self.accept_mutations = False
        self.stopping = False
        self.run_task: asyncio.Task | None = None
        self.in_flight: dict[asyncio.Task, InputReceipt] = {}
        self.workers: list[asyncio.Task] = []
        self.final_result = RunResult()
        self.sequence = 0
        self.clarification: InputReceipt | None = None
        self.frontend.bind(self)

    @property
    def input_binding(self):
        return self.binding or self.last_binding

    @property
    def pending_count(self) -> int:
        return sum(
            r.state in {"queued", "classifying", "deferred", "awaiting_clarification"}
            for r in self.receipts.values()
        )

    @property
    def input_available(self) -> bool:
        return self.frontend.interactive and not self.closing.is_set()

    def set_phase(self, phase: SessionPhase, activity: str | None = None) -> None:
        if self.phase == SessionPhase.CLOSED:
            return
        self.phase = phase
        if activity is not None:
            self.activity = safe_text(activity)
        self.frontend.invalidate()

    def submit(self, text: str, *, binding=_UNSET, approval_id=_UNSET) -> bool:
        if not text.strip():
            return True
        if self.closing.is_set() or self.output.overloaded:
            self.output.emit("입력 미접수: 세션 종료/출력 복구 중입니다.")
            return False
        parsed = command(text)
        control = parsed and parsed[0] in CONTROL_COMMANDS
        queue = self.controls if control else self.queue
        if (
            len(text) > 12000
            or queue.full()
            or self.pending_count >= INPUT_CAPACITY + CONTROL_CAPACITY
        ):
            self.output.emit("입력 미접수: 입력 길이/대기 한도에 도달했습니다. 초안을 보존합니다.")
            return False
        # Only completed, unreferenced receipts may expire.
        if len(self.receipts) >= RECEIPT_CAPACITY:
            for key, receipt in list(self.receipts.items()):
                if receipt.state in {"handled", "not_applied"}:
                    del self.receipts[key]
                    break
            else:
                self.output.emit("입력 미접수: 미확정 요청을 먼저 확인하세요.")
                return False
        self.sequence += 1
        env = InputEnvelope(
            f"input_{self.sequence}",
            text,
            self.sequence,
            self.phase,
            self.input_binding,
            None if binding is _UNSET else binding,
            binding is not _UNSET,
            self.pending_approval if approval_id is _UNSET else approval_id,
        )
        receipt = InputReceipt(env)
        self.receipts[env.input_id] = receipt
        queue.put_nowait(receipt)
        self.output.emit(Text("❯ " + safe_text(text), style="bold cyan"))
        self.frontend.invalidate()
        return True

    async def join_inputs(self) -> None:
        await self.queue.join()
        await self.controls.join()
        await self.idle.wait()

    def request_close(self) -> None:
        self.closing.set()

    async def run(self, initial_objective: str | None = None) -> RunResult:
        self.output.start()
        self.workers = [
            asyncio.create_task(self._consume(self.queue), name="cli-chat"),
            asyncio.create_task(self._consume(self.controls), name="cli-control"),
        ]
        reader = asyncio.create_task(self.frontend.run(), name="cli-reader")
        try:
            self.output.emit(
                f"REDTEAM · {safe_text(self.engagement_name)} · "
                f"scope {safe_text(', '.join(self.scope))}"
                + (f" · model {safe_text(self.model_label)}" if self.model_label else "")
            )
            if not self.frontend.interactive:
                self.output.emit(
                    "순차 입력 모드: 실행 중 입력은 지원하지 않습니다. 승인 대기는 보존됩니다."
                )
            if initial_objective:
                self.submit(initial_objective)
                # Non-TTY compatibility must not read the next line before the initial request.
                self.idle.clear()
            await self.closing.wait()
        finally:
            try:
                await self.close()
            finally:
                self.frontend.close()
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
        return self.final_result

    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            receipt = await queue.get()
            try:
                if receipt.state == "not_applied":
                    continue
                await self.handle(receipt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._receipt(receipt, "not_applied", f"입력 처리 실패: {type(exc).__name__}")
            finally:
                queue.task_done()
                if self.run_task is None and not self.deferred:
                    self.idle.set()

    def _receipt(self, receipt: InputReceipt, state: str, reason: str) -> None:
        # Unknown side effects must not be overwritten by worker cancellation.
        receipt.state, receipt.reason = state, safe_text(reason)
        self.output.emit(f"[Input] {receipt.envelope.input_id} · {receipt.reason}")
        self.frontend.invalidate()

    def _same(self, binding: BindingRef | None) -> bool:
        return bool(
            binding and self.binding and binding.attachment_id == self.binding.attachment_id
        )

    async def handle(self, receipt: InputReceipt) -> None:
        if receipt.state != "queued":
            return
        if self.closing.is_set():
            self._receipt(receipt, "not_applied", "세션 종료로 미전달")
            return
        env = receipt.envelope
        parsed = command(env.text)
        if parsed:
            await self._command(receipt, *parsed)
            return
        receipt.state = "classifying"
        bound = env.binding
        context = (
            f"Input binding: {bound}. Current binding: {self.binding}. Phase: {self.phase.value}."
        )
        if bound:
            context += self.snapshots.get(bound.attachment_id, "")
        if self._same(bound) and self.operator is not None:
            context += await self.operator.session_context()
            context += "\nRecent public events:\n" + "\n".join(self.recent_events)
        context += f"\nPending approval: {self.pending_approval}; deferred: {len(self.deferred)}."
        if self.clarification:
            context += (
                f"\nUnresolved request {self.clarification.envelope.input_id}: "
                f"{self.clarification.envelope.text}. A question is not confirmation."
            )
        for attempt in range(2):
            revision = self.context_loader() if self.context_loader else None
            decision = await self.router.route(
                env.text,
                context=context,
                active=self._same(bound),
                engagement_name=self.engagement_name,
                scope=self.scope,
            )
            latest = self.context_loader() if self.context_loader else None
            if latest == revision:
                receipt.context_revision = revision
                break
            if attempt:
                self._receipt(receipt, "not_applied", "설정이 계속 변경되어 실행하지 않았습니다.")
                return
        if receipt.state == "not_applied" or self.closing.is_set():
            return
        receipt.decision = decision
        response = decision.response
        if decision.mode == "answer" and bound and not self._same(bound):
            # Classification may have raced the terminal return. Do not present
            # a pre-terminal answer as the latest state of the original run.
            latest = self.snapshots.get(bound.attachment_id)
            if latest and latest not in context:
                response = "분류 중 원래 Run이 종료되었습니다.\n" + latest
        self.output.emit(Markdown(safe_text(response), hyperlinks=False))
        if decision.mode == "answer":
            receipt.state = "handled"
        elif decision.mode == "clarify":
            receipt.state = "awaiting_clarification"
            self.clarification = receipt
        elif decision.mode == "guidance" or decision.investigation == "current":
            await self._mutate(
                receipt, "guidance", decision.guidance or decision.objective or env.text
            )
        else:
            if self.clarification:
                self._receipt(self.clarification, "handled", f"후속 확인: {env.input_id}")
                self.clarification = None
            await self._new_run(receipt, decision.objective or env.text)

    async def _command(self, receipt: InputReceipt, name: str, rest: str) -> None:
        if name == "exit":
            self._receipt(receipt, "handled", "세션 종료 요청됨")
            self.request_close()
        elif name in {
            "approve",
            "deny",
            "stop",
            "pause",
            "resume",
            "focus",
            "exclude",
            "reprioritize",
        }:
            if (
                self.run_task is None
                and self.resume_runner
                and name in {"approve", "deny", "resume"}
                and self.phase in {SessionPhase.WAITING_APPROVAL, SessionPhase.PAUSED}
            ):
                await self._resume_run(receipt, name, rest)
            else:
                await self._mutate(receipt, name, rest)
        elif name in {"status", "explain"}:
            if rest and self.slash_handler:
                await self.slash_handler(name, rest, self)
                receipt.state = "handled"
                return
            bound = receipt.envelope.binding
            if self._same(bound) and self.operator:
                self.output.emit(await self.operator.session_context())
            else:
                self.output.emit(
                    self.snapshots.get(bound.attachment_id, "아직 실행 기록이 없습니다.")
                    if bound
                    else "아직 실행 기록이 없습니다."
                )
            receipt.state = "handled"
        elif name == "trace":
            self.output.emit("\n".join(self.output.history))
            receipt.state = "handled"
        elif name == "pending":
            self.output.emit(
                "\n".join(
                    f"{key}: {r.state} · {safe_text(r.envelope.text, limit=120)}"
                    for key, r in self.receipts.items()
                    if r.state not in {"handled", "not_applied"}
                )
                or "보류 요청 없음"
            )
            receipt.state = "handled"
        elif name == "cancel":
            target = self.receipts.get(rest)
            if target and target.state in {
                "queued",
                "classifying",
                "deferred",
                "awaiting_clarification",
            }:
                self._receipt(target, "not_applied", "사용자가 미전달 요청을 취소했습니다.")
                if self.clarification is target:
                    self.clarification = None
            else:
                self.output.emit(
                    "취소 불가: 미전달 요청 ID를 지정하세요. 전달된 요청은 재실행하지 않습니다."
                )
            receipt.state = "handled"
        elif name == "new":
            if rest:
                await self._new_run(receipt, rest)
            else:
                self._receipt(receipt, "not_applied", "사용법: /new <새 조사 요청>")
        elif name == "help":
            self.output.emit(
                "Commands · 자연어 질문/추가 지시/새 조사 요청을 입력하세요.\n"
                + "\n".join("/" + n + "  " + self.command_help.get(n, "") for n in self.commands)
                + "\nEnter 제출 · Ctrl+J 줄바꿈 · Ctrl+C 초안 취소 · Ctrl+D 빈 입력에서 종료"
            )
            receipt.state = "handled"
        elif self.slash_handler:
            await self.slash_handler(name, rest, self)
            receipt.state = "handled"
        else:
            self._receipt(receipt, "not_applied", "알 수 없는 명령입니다. /help")

    async def _new_run(self, receipt: InputReceipt, objective: str) -> None:
        if self.single_run and self.last_binding:
            self._receipt(
                receipt, "not_applied", "단일 실행 모드: 별도 조사는 새 CLI에서 시작하세요."
            )
            return
        receipt.decision = objective
        async with self.gate:
            if self.closing.is_set():
                self._receipt(receipt, "not_applied", "세션 종료로 새 조사를 시작하지 않았습니다.")
                return
            if self.run_task is not None or self.phase not in {
                SessionPhase.IDLE,
                SessionPhase.STARTING,
            }:
                if receipt.state != "deferred":
                    self.deferred.append(receipt)
                self._receipt(receipt, "deferred", "새 조사 요청 보류 — 현재 Run 정리 후 시작")
                return
            self.idle.clear()
            self.ready.clear()
            self.stopping = False
            self.recent_events.clear()
            self.terminal_event_reason = None
            self.binding = BindingRef(receipt.envelope.input_id, uuid4().hex)
            self.active_receipt = receipt
            self.set_phase(SessionPhase.STARTING, "실행 설정과 scope 확인 중")
            receipt.state = "dispatched"
            self.run_task = asyncio.create_task(self._execute(objective, receipt), name="cli-run")

    async def _execute(self, objective: str, receipt: InputReceipt) -> None:
        binding = self.binding
        try:
            if isinstance(objective, tuple):
                result = await self.resume_runner(*objective, self)
            else:
                result = await self.runner(objective, self)
        except asyncio.CancelledError:
            result = RunResult(exit_code=1, error="실행 연결 종료 — backend 처리 여부 확인 필요")
        except Exception as exc:
            result = RunResult(exit_code=1, error=safe_text(f"{type(exc).__name__}: {exc}"))
        await self.detach()
        binding = self.binding or binding
        final = result.final
        if final is not None:
            binding = replace(binding, run_id=str(final.run_id))
            if self.on_finished:
                try:
                    self.on_finished(final)
                except Exception as exc:
                    result.exit_code = 1
                    result.error = f"CLI 결과 projection 갱신 실패: {type(exc).__name__}"
        self.final_result = result
        view = RunCompletionView.from_result(binding, result)
        if self.terminal_event_reason and self.terminal_event_reason != view.reason:
            self.output.emit(
                f"[진단] 종료 이벤트 reason={self.terminal_event_reason}; "
                f"최종 반환 reason={view.reason}. 최종 반환 상태로 표시합니다."
            )
        title = {
            "ended": "실행 종료",
            "failed": "실행 실패",
            "suspended": "실행 대기",
            "unknown": "실행 상태 확인 불가",
        }[view.outcome]
        lines = [
            f"{title} · RunId: {binding.run_id or '시작 전'}",
            *(safe_text(item) for item in view.observations),
            f"Phase: {view.phase or 'unknown'} · "
            f"종료 이유: {view.reason or view.error or 'unknown'}",
            f"Mission: {view.mission_status or 'unknown'} · Actions executed: {view.actions}",
        ]
        if view.error:
            lines.append(view.error)
        if binding.run_id and view.outcome != "suspended":
            lines.append(f"Report: redteam report generate {binding.run_id}")
        summary = "\n".join(lines)
        self.snapshots[binding.attachment_id] = summary
        while len(self.snapshots) > 64:
            self.snapshots.popitem(last=False)
        await self.output.fence()
        self.output.emit(
            Text(safe_text(summary), style="red" if view.outcome == "failed" else "default")
        )
        await self.output.fence()
        self.last_binding = binding
        self.binding = None
        self.run_task = None
        self.pending_approval = (
            str(final.pending_approval_id)
            if final is not None and getattr(final, "pending_approval_id", None)
            else None
        )
        receipt.state = "handled"
        phase = (
            SessionPhase.WAITING_APPROVAL
            if view.reason == "approval_pending"
            else SessionPhase.PAUSED
            if view.reason == "operator_paused"
            else SessionPhase.IDLE
        )
        self.set_phase(phase, "입력 대기 — 현재 결과를 질문하거나 새 조사를 요청하세요.")
        if phase == SessionPhase.WAITING_APPROVAL:
            self.activity = f"승인 대기: {self.pending_approval} · approve <ID> / deny <ID>"
        elif phase == SessionPhase.PAUSED:
            self.activity = "일시 중단 · /resume으로 같은 Run 재개"
        self.idle.set()
        if self.single_run:
            self.request_close()
        elif not self.closing.is_set() and phase == SessionPhase.IDLE:
            while self.deferred:
                next_receipt = self.deferred.popleft()
                if next_receipt.state == "deferred":
                    await self._new_run(next_receipt, next_receipt.decision)
                    break

    async def _resume_run(self, receipt, kind, value):
        env = receipt.envelope
        async with self.gate:
            if (
                self.closing.is_set()
                or self.run_task is not None
                or not env.binding
                or not self.last_binding
                or env.binding.attachment_id != self.last_binding.attachment_id
            ):
                self._receipt(receipt, "not_applied", "원래 Run과 재개 대상이 다릅니다.")
                return
            if kind in {"approve", "deny"}:
                value = value or env.approval_id
                if not value or value != env.approval_id or value != self.pending_approval:
                    self._receipt(receipt, "not_applied", "승인 대상이 변경되었습니다.")
                    return
            self.binding = BindingRef(env.input_id, uuid4().hex, self.last_binding.run_id)
            self.idle.clear()
            self.ready.clear()
            self.stopping = False
            self.recent_events.clear()
            self.terminal_event_reason = None
            self.set_phase(SessionPhase.STARTING, "원래 Run의 안전한 checkpoint 확인 중")
            receipt.state = "dispatched"
            self.run_task = asyncio.create_task(
                self._execute((kind, value, self.last_binding.run_id), receipt), name="cli-resume"
            )

    def attach(self, operator) -> None:
        self.operator = operator
        self.accept_mutations = True

    def observe(self, event) -> None:
        if not self.binding or not self.accept_mutations:
            return
        if self.binding.run_id and str(event.run_id) != self.binding.run_id:
            return
        self.binding = replace(self.binding, run_id=str(event.run_id))
        kind = getattr(event, "event_type", "")
        payload = getattr(event, "payload", {})
        if kind == "RunFinished":
            self.terminal_event_reason = payload.get("termination_reason")
        if kind in {
            "ToolExecutionFinished",
            "ToolExecutionFailed",
            "ActionDenied",
            "ApprovalRequired",
            "AgentTurnDecided",
            "GraphUpdated",
        }:
            self.recent_events.append(safe_text(f"{kind}: {payload}", limit=250))
        if self.operator and self.operator._identity.mission_id is not None:
            self.ready.set()
        if self.phase == SessionPhase.STARTING:
            self.set_phase(SessionPhase.RUNNING, "조사 진행 중")

    def suspended(self, final) -> None:
        self.pending_approval = (
            str(final.pending_approval_id) if final.pending_approval_id else None
        )
        self.set_phase(
            SessionPhase.WAITING_APPROVAL if self.pending_approval else SessionPhase.PAUSED,
            f"승인 필요: {self.pending_approval} · approve <ID> / deny <ID>"
            if self.pending_approval
            else "일시 중단 · /resume 또는 /stop",
        )

    def resumed(self) -> None:
        self.pending_approval = None
        self.set_phase(SessionPhase.RUNNING, "실행 상태 확인 중")

    async def _mutate(self, receipt: InputReceipt, kind: str, value: str) -> None:
        env = receipt.envelope
        if self._same(env.binding) and self.phase == SessionPhase.STARTING:
            try:
                await asyncio.wait_for(self.ready.wait(), DISPATCH_TIMEOUT)
            except asyncio.TimeoutError:
                self._receipt(
                    receipt, "not_applied", "시작 중: 아직 실행 채널이 준비되지 않았습니다."
                )
                return
        async with self.gate:
            if (
                self.closing.is_set()
                or not self._same(env.binding)
                or not self.accept_mutations
                or not self.operator
                or (self.stopping and kind not in {"stop"})
            ):
                self._receipt(
                    receipt,
                    "not_applied",
                    "원래 Run에 전달되지 않음 — 종료/변경된 문맥입니다. 후속 조사는 /new <요청>",
                )
                return
            if kind in {"approve", "deny"}:
                approval = value or env.approval_id
                if not approval or approval != env.approval_id or approval != self.pending_approval:
                    self._receipt(
                        receipt,
                        "not_applied",
                        "승인 대상이 변경되었거나 없습니다. /approvals로 확인하세요.",
                    )
                    return
                value = approval
            if receipt.state == "dispatched":
                return
            receipt.state = "dispatched"
            operator = self.operator
            run_id = self.binding.run_id

            async def dispatch():
                if kind in {"approve", "deny"} and self.approval_verifier is not None:
                    if not await self.approval_verifier(value, run_id):
                        return False, "저장된 승인 상태/Run이 일치하지 않아 변경하지 않았습니다."
                return await operator.dispatch(kind, value)

            if kind == "stop":
                self.stopping = True
            task = asyncio.create_task(dispatch(), name=f"cli-{env.input_id}")
            self.in_flight[task] = receipt
        try:
            applied, reason = await asyncio.wait_for(asyncio.shield(task), DISPATCH_TIMEOUT)
            self._receipt(receipt, "handled" if applied else "not_applied", reason)
            if kind == "stop" and applied:
                self.stopping = True
        except (Exception, asyncio.CancelledError) as exc:
            self._receipt(receipt, "unknown", "처리 여부 확인 불가 — 자동 재전송하지 않습니다.")
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if task.done():
                self.in_flight.pop(task, None)

    async def detach(self) -> None:
        async with self.gate:
            self.accept_mutations = False
            self.ready.set()
            self.set_phase(SessionPhase.SETTLING, "결과 정리 중 — 입력은 보존됩니다.")
        tasks = list(self.in_flight)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=DISPATCH_TIMEOUT)
            for task in tasks:
                receipt = self.in_flight.pop(task, None)
                if task in pending:
                    if receipt:
                        self._receipt(
                            receipt, "unknown", "응답 확인 시간 초과 — 적용 여부 확인 필요"
                        )
                    task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    applied, reason = await task
                    if receipt and receipt.state == "unknown" and task not in pending:
                        self._receipt(
                            receipt,
                            "handled" if applied else "not_applied",
                            "늦은 응답 확인: " + reason,
                        )
        self.operator = None
        self.pending_approval = None

    async def close(self) -> None:
        self.closing.set()
        self.set_phase(SessionPhase.CLOSING, "미처리 입력과 실행 자원 정리 중")
        shutdown_failed = False
        if self.operator and self.accept_mutations and not self.stopping:
            try:
                await asyncio.wait_for(self.operator.dispatch("stop", ""), DISPATCH_TIMEOUT)
            except Exception:
                shutdown_failed = True
                self.output.emit("Run 중단 확인 불가 — 상태를 별도로 확인하세요.")
        running = self.run_task
        if running:
            try:
                await asyncio.wait_for(asyncio.shield(running), CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                shutdown_failed = True
                self.output.emit("종료 시간 초과 — Run/진행 중 요청의 처리 여부가 미확정입니다.")
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running
        if shutdown_failed:
            self.final_result.exit_code = 1
        for task in self.workers:
            task.cancel()
        for task in self.workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for receipt in self.receipts.values():
            if receipt.state in {"queued", "classifying", "deferred", "awaiting_clarification"}:
                self._receipt(receipt, "not_applied", "세션 종료로 미전달")
        try:
            await self.output.close()
        except Exception as exc:
            self.final_result.exit_code = 1
            self.frontend.emergency(f"최종 출력 정리 실패: {type(exc).__name__}")
        finally:
            self.set_phase(SessionPhase.CLOSED)
