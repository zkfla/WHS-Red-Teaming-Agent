"""Web frontend for ``SessionController``: structured items instead of a terminal.

The controller already owns inputs, receipts, phases and the pending approval.
This frontend only observes them (``invalidate`` is called after every change)
and records display items. It never submits, approves or classifies anything.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TextIO

from redteam.app.cli.conversation import conversation_failure_label
from redteam.app.cli.display import safe_text
from redteam.app.web.events import EventLog, runtime_event_data
from redteam.orchestration.diagnostics import DecisionDiagnostic

_SENT_LIMIT = 1024
_DECISION_TEXT = 2000
POLL_INTERVAL = 0.2

#: Bounded scalar fields flattened from a receipt diagnostic so the nested
#: cause rule/location survive the event-log depth bound. No provider
#: bodies, prompts, keys, or exception messages travel here.
_DIAGNOSTIC_TEXT_LIMIT = 200
_DIAGNOSTIC_LIST_LIMIT = 3


def binding_data(binding: Any) -> dict[str, str | None] | None:
    if binding is None:
        return None
    return {
        "start_request_id": str(binding.start_request_id),
        "attachment_id": str(binding.attachment_id),
        "run_id": str(binding.run_id) if binding.run_id else None,
    }


def _decision_data(decision: Any) -> dict[str, Any] | None:
    if hasattr(decision, "mode") and hasattr(decision, "response"):
        return {
            "mode": str(decision.mode),
            "investigation": getattr(decision, "investigation", None),
            "response": safe_text(decision.response, limit=_DECISION_TEXT),
            "plan": [safe_text(item, limit=500) for item in list(decision.plan)[:6]],
            "assumptions": [safe_text(item, limit=500) for item in list(decision.assumptions)[:5]],
        }
    if isinstance(decision, str):
        return {"mode": "execute", "objective": safe_text(decision, limit=1000)}
    return None


def _scalar(value: Any) -> str:
    """One bounded display scalar; absent/blank stays absent, never 'None'."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return ""
    return safe_text(text, limit=_DIAGNOSTIC_TEXT_LIMIT)


def _diagnostic_view(diagnostic: Any) -> dict[str, Any] | None:
    """Flattened public diagnostic projection for an input receipt.

    The full ``DecisionDiagnostic`` stays on the receipt; only bounded
    cause/rule/location/provider-status scalars travel here so the
    nested ``last_cause.validation_issues`` survive the event-log depth
    bound (events.DEPTH_LIMIT) without relaxing it. Every field is a
    shallow scalar: no nested dicts, no lists of dicts.
    """
    if diagnostic is None:
        return None
    dump = diagnostic.model_dump(mode="json") if hasattr(diagnostic, "model_dump") else {}
    if not isinstance(dump, dict):
        return None
    typed_dump: dict[str, Any] = dump
    cause_raw = typed_dump.get("last_cause")
    cause: dict[str, Any] = cause_raw if isinstance(cause_raw, dict) else {}
    issues_raw = typed_dump.get("validation_issues")
    issues: list[Any] = issues_raw if isinstance(issues_raw, list) else []
    cause_issues_raw = cause.get("validation_issues")
    cause_issues: list[Any] = (
        cause_issues_raw if isinstance(cause_issues_raw, list) else []
    )

    def first_rule(rows: Any) -> str:
        for row in rows[:_DIAGNOSTIC_LIST_LIMIT]:
            if isinstance(row, dict) and str(row.get("rule") or "").strip():
                return _scalar(row.get("rule"))
        return ""

    cause_code = _scalar(cause.get("code")) if cause.get("code") else ""
    status = dump.get("provider_status")
    if status is None:
        status = cause.get("provider_status")
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    attempts = dump.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = 0
    retry_after = dump.get("retry_after")
    if retry_after is None:
        retry_after = cause.get("retry_after")
    if (not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool)
            or retry_after < 0):
        retry_after = None
    retryable = dump.get("retryable")
    if cause and dump.get("last_cause") is not None:
        retryable = cause.get("retryable", retryable)
    if not isinstance(retryable, bool):
        retryable = False
    view: dict[str, Any] = {
        "code": _scalar(dump.get("code")),
        "stage": _scalar(dump.get("stage")),
        "kind": _scalar(dump.get("kind")),
        "attempts": attempts,
        "location": _scalar(dump.get("location") or cause.get("location")),
        "rule": first_rule(issues) or first_rule(cause_issues),
        "cause_code": cause_code,
        "provider_status": status,
        "provider_code": _scalar(
            dump.get("provider_code") or cause.get("provider_code")
        ),
        "retryable": retryable,
        "retry_after": retry_after,
        "repair_condition": _scalar(
            dump.get("repair_condition") or cause.get("repair_condition")
        ),
        "label": safe_text(
            conversation_failure_label(diagnostic, fallback="unknown"),
            limit=_DIAGNOSTIC_TEXT_LIMIT * 2,
        ),
    }
    return {key: value for key, value in view.items() if value not in ("", None)}


class WebFrontend:
    interactive = True

    def __init__(
        self,
        log: EventLog,
        *,
        on_bind: Callable[[Any], None] | None = None,
        mirror: TextIO | None = None,
    ) -> None:
        self.log = log
        # While a session runs, stdout/stderr are captured into console items. Mirroring
        # them to the launching terminal keeps messages meant for the operator there
        # (for example a device sign-in prompt) visible. Text is already masked.
        self._mirror = mirror
        self.controller: Any = None
        self.details = False
        self.closed = False
        self._on_bind = on_bind
        self._state_signature: tuple | None = None
        self._sent: OrderedDict[str, tuple] = OrderedDict()

    # -- frontend interface used by SessionController -------------------

    def bind(self, controller: Any) -> None:
        self.controller = controller
        if self._on_bind is not None:
            self._on_bind(controller)
        self.invalidate()

    def invalidate(self) -> None:
        controller = self.controller
        if controller is None or self.closed:
            return
        self._publish_inputs(controller)
        self._publish_state(controller)

    async def run(self) -> None:
        # Input arrives through the HTTP API. The controller changes some receipt
        # states (for example answered questions) without calling ``invalidate``
        # because the terminal redraws on its own, so the web view re-reads the
        # controller on a short interval for as long as the session lives.
        closing = self.controller.closing
        while not closing.is_set():
            self.invalidate()
            try:
                await asyncio.wait_for(closing.wait(), POLL_INTERVAL)
            except asyncio.TimeoutError:
                continue

    def close(self) -> None:
        if self.controller is not None:
            self._publish_inputs(self.controller)
            self._publish_state(self.controller)
        self.closed = True

    async def write(self, content: Any, console: Any) -> None:
        self.invalidate()
        text = getattr(content, "plain", None)
        if text is None:
            text = getattr(content, "markup", None)
        if text is None:
            text = str(content)
        text = safe_text(text, limit=4000).strip()
        if text:
            self.log.append("console", {"text": text, "level": "info"})
            self._echo(text)

    def emergency(self, text: str) -> None:
        text = safe_text(text, limit=2000)
        self.log.append("console", {"text": text, "level": "error"})
        self._echo(text)

    def _echo(self, text: str) -> None:
        if self._mirror is None:
            return
        try:
            print(text, file=self._mirror, flush=True)
        except Exception:  # noqa: BLE001 - a broken terminal must not stop the session
            self._mirror = None

    # -- structured runtime events (SessionProjection sink) -------------

    def event_sink(self, event: Any) -> None:
        data, run_id = runtime_event_data(event)
        self.log.append("event", data, run_id=run_id)

    # -- observation of controller state --------------------------------

    def state_data(self) -> dict[str, Any] | None:
        controller = self.controller
        if controller is None:
            return None
        last = controller.last_binding
        summary = controller.snapshots.get(last.attachment_id) if last else None
        return {
            "phase": controller.phase.value,
            "activity": safe_text(controller.activity, limit=1000),
            "pending_approval": controller.pending_approval,
            "pending_approvals": list(controller.pending_approvals),
            "binding": binding_data(controller.input_binding),
            "active_run": controller.run_task is not None,
            "termination_reason": controller.terminal_event_reason,
            "engagement": safe_text(controller.engagement_name, limit=200),
            "scope": [safe_text(item, limit=200) for item in list(controller.scope)[:50]],
            "model": safe_text(controller.model_label, limit=200),
            "last_summary": safe_text(summary, limit=4000) if summary else None,
        }

    def _publish_state(self, controller: Any) -> None:
        data = self.state_data()
        if data is None:
            return
        signature = (
            data["phase"], data["activity"], tuple(data["pending_approvals"]),
            repr(data["binding"]), data["active_run"], data["last_summary"],
            data["termination_reason"],
        )
        if signature == self._state_signature:
            return
        self._state_signature = signature
        self.log.append("state", data)

    def _publish_inputs(self, controller: Any) -> None:
        for key, receipt in list(controller.receipts.items()):
            envelope = receipt.envelope
            decision = _decision_data(receipt.decision)
            diagnostic = getattr(receipt, "diagnostic", None)
            if diagnostic is not None and not isinstance(diagnostic, DecisionDiagnostic):
                diagnostic = None
            diagnostic_view = _diagnostic_view(diagnostic)
            signature = (
                receipt.state, receipt.reason,
                decision.get("mode") if decision else None,
                decision.get("response") if decision else None,
                # Immutable snapshot of every bounded view field: a changed
                # rule/location/status republishes even when the truncated
                # label text is identical.
                json.dumps(diagnostic_view, sort_keys=True) if diagnostic_view else None,
            )
            if self._sent.get(key) == signature:
                continue
            self._sent[key] = signature
            self._sent.move_to_end(key)
            while len(self._sent) > _SENT_LIMIT:
                self._sent.popitem(last=False)
            reason = safe_text(receipt.reason, limit=500)
            if diagnostic is not None and (
                not reason.strip() or reason.strip() in {"ConversationError"}
            ):
                reason = diagnostic_view.get("label", "unknown") if diagnostic_view else "unknown"
            self.log.append(
                "input",
                {
                    "input_id": str(envelope.input_id),
                    "text": safe_text(envelope.text, limit=4000),
                    "state": receipt.state,
                    "reason": reason,
                    "received_at": envelope.received_at.isoformat(),
                    "binding": binding_data(envelope.binding),
                    "decision": decision,
                    "diagnostic": diagnostic_view,
                },
            )
