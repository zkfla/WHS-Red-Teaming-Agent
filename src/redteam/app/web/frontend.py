"""Web frontend for ``SessionController``: structured items instead of a terminal.

The controller already owns inputs, receipts, phases and the pending approval.
This frontend only observes them (``invalidate`` is called after every change)
and records display items. It never submits, approves or classifies anything.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TextIO

from redteam.app.cli.display import safe_text
from redteam.app.web.events import EventLog, runtime_event_data

_SENT_LIMIT = 1024
_DECISION_TEXT = 2000
POLL_INTERVAL = 0.2


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
            "binding": binding_data(controller.input_binding),
            "active_run": controller.run_task is not None,
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
            data["phase"], data["activity"], data["pending_approval"],
            repr(data["binding"]), data["active_run"], data["last_summary"],
        )
        if signature == self._state_signature:
            return
        self._state_signature = signature
        self.log.append("state", data)

    def _publish_inputs(self, controller: Any) -> None:
        for key, receipt in list(controller.receipts.items()):
            envelope = receipt.envelope
            decision = _decision_data(receipt.decision)
            signature = (
                receipt.state, receipt.reason,
                decision.get("mode") if decision else None,
                decision.get("response") if decision else None,
            )
            if self._sent.get(key) == signature:
                continue
            self._sent[key] = signature
            self._sent.move_to_end(key)
            while len(self._sent) > _SENT_LIMIT:
                self._sent.popitem(last=False)
            self.log.append(
                "input",
                {
                    "input_id": str(envelope.input_id),
                    "text": safe_text(envelope.text, limit=4000),
                    "state": receipt.state,
                    "reason": safe_text(receipt.reason, limit=500),
                    "received_at": envelope.received_at.isoformat(),
                    "binding": binding_data(envelope.binding),
                    "decision": decision,
                },
            )
