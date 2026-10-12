"""Persistent interactive session (asd.md / prior pasted spec sections
4-5, 12-15, 19): a Run ending must not end the CLI process, slash
commands must reuse the same Application Services as the standalone
subcommands, and a second natural-language request must go through the
same Runtime path as the first -- all through the real
``redteam.app.cli.commands.interactive.run()`` entry point, exactly like
``tests/integration/test_cli_interactive.py`` (same in-process
terminal and runtime conversation/model doubles -- the Agent
Runtime, Control Plane, and Capability Gateway run for real against an
honest empty catalog (no MCP servers configured). The execution model is
an explicitly injected hold plan and the Intake Assistant uses its own
explicit scripted double; the production LLM builder is never patched).

Covers section 29's "Integration"/"CLI E2E" requirements for this
feature: Interactive -> Application, Slash command -> Shared Service,
Run lifecycle -> Session lifecycle, and the full
``/help`` -> natural language -> ``/status`` -> ``/tools`` -> ``/exit``
scenario in one process.
"""

from __future__ import annotations

import contextlib
import json
from io import StringIO

import pytest
from rich.console import Console

from redteam.orchestration.state import RuntimeState

pytestmark = pytest.mark.asyncio

_COMPLETE_TURN = {
    "draft": {
        "name": "VulnBank Local",
        "allowed_hosts": ["127.0.0.1:5000"],
        "max_actions": 100,
    },
    "acknowledgement": "확인했습니다.",
    "follow_up_questions": [],
    "ready": True,
}


class _FakeConsole:
    """Same shape as ``test_cli_interactive.py``'s own fake."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.transcript: list[str] = []
        self.finished_runs: list[RuntimeState] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        for arg in args:
            if isinstance(arg, str):
                self.transcript.append(arg)
            else:
                output = StringIO()
                Console(file=output, width=140, color_system=None).print(arg)
                self.transcript.append(output.getvalue())

    def input(self, prompt: str = "") -> str:
        self.transcript.append(str(prompt))
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)

    def status(self, *_args: object, **_kwargs: object):  # noqa: ANN201
        return contextlib.nullcontext()

    def clear(self) -> None:
        self.transcript.append("<cleared>")




class _ConversationLLM:
    """Explicit conversation decisions, independent of intake and engine scripts."""

    async def complete_structured(self, *, messages, **kwargs):
        payload = json.loads(messages[0]["content"])
        message = payload["user_message"]
        if message in {"방금 왜 실패했어?", "결과에서 확인된 내용이 뭐야?"}:
            return {
                "mode": "answer",
                "response": "[Agent] Run summary because " + payload["session_context"],
            }
        return {"mode": "execute", "response": "등록된 범위를 조사합니다.", "objective": message}

    async def complete(self, **kwargs):
        raise AssertionError("conversation uses structured decisions only")








# --- Gate F / A: RunFinished returns to prompt, session stays alive -------












# --- Gate I: error recovery ------------------------------------------------




# --- Gate K: clean exit on EOF while idle at the prompt --------------------
