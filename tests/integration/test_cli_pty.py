"""Real PTY + production session/execute adapters; no external LLM/MCP calls."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from redteam.core.config import OrchestrationConfig


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.delenv("REDTEAM_RUNTIME_BACKEND", raising=False)


async def _fixture():
    from rich.console import Console

    from redteam.app.cli.conversation import ConversationAssistant
    from redteam.app.cli.interactive_commands import InteractiveSession
    from redteam.app.cli.session_runtime import run_session
    from redteam.app.cli.terminal_frontend import TerminalFrontend
    from redteam.core.config import AppConfig, EngagementConfig
    from redteam.core.contracts.events import EventType
    from redteam.core.enums import AgentPhase
    from redteam.event_bus.publisher import EventPublisher
    from redteam.orchestration.state import RuntimeState
    from tests.unit.test_cli_operator_console import ENG, MISSION, RUN, _console

    operator, *_ = await _console()
    deps = operator._deps
    # RuntimeDeps always carries tool_catalog (None means no catalog). The console
    # stub predates that field, and the session's tool context reads it directly.
    deps.tool_catalog = getattr(deps, "tool_catalog", None)
    end = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGUSR1, end.set)
    frontend = TerminalFrontend()

    def probe():
        assert frontend.draft_binding.run_id == str(RUN)
        frontend.controller.output.emit(
            f"BUFFER {frontend.buffer.text} CURSOR {frontend.buffer.cursor_position}"
        )

    loop.add_signal_handler(signal.SIGUSR2, probe)

    class LLM:
        async def complete_structured(self, **kwargs):
            import json

            payload = json.loads(kwargs["messages"][0]["content"])
            return {"mode": "answer", "response": "ACK:" + payload["user_message"]}

    async def controlled(engagement, **kwargs):
        publisher = EventPublisher(
            deps.event_bus, run_id=RUN, engagement_id=ENG, source="pty-fixture"
        )
        await publisher.publish(EventType.RUN_STARTED, {})
        await publisher.publish(EventType.MISSION_CREATED, {"mission_id": str(MISSION)})
        await publisher.publish("AgentTurnDecided", {"response": "PTY_READY"})
        await end.wait()
        # A terminal event is not itself a controller completion.
        await publisher.publish(
            EventType.RUN_FINISHED, {"termination_reason": "no_actionable_plan"}
        )
        return RuntimeState(
            run_id=RUN,
            engagement_id=ENG,
            mission_id=MISSION,
            phase=AgentPhase.FINISHED,
            termination_reason="no_actionable_plan",
        )

    from redteam.app import central_run
    central_run.run_central_engagement = controlled
    engagement = EngagementConfig.model_validate(
        {
            "name": "PTY fixture",
            "scope": {"allowed_hosts": ["localhost"]},
            "execution_budget": {"max_actions": 3},
        }
    )
    config = AppConfig(
        orchestration=OrchestrationConfig(), engagement=engagement,
    )
    session = InteractiveSession(engagement, config.llm, engagement_path=Path("uncreated.yaml"))
    result = await run_session(
        Console(),
        config,
        conversation=ConversationAssistant(LLM()),
        session=session,
        objective="/new localhost 조사",
        frontend=frontend,
        built=(engagement, deps),
    )
    print(f"PTY_EXIT {result.exit_code}")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY fixture")
def test_live_draft_cursor_survives_terminal_result_and_next_question(tmp_path):
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios
    import time

    master, slave = pty.openpty()
    width = int(os.environ.get("WHS_TEST_PTY_WIDTH", "80"))
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, width, 0, 0))
    root = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items() if not key.startswith("REDTEAM_")}
    env.update(TERM="xterm-256color", PYTHONPATH=str(root), PROMPT_TOOLKIT_NO_CPR="1")
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--fixture"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=tmp_path,
        env=env,
    )
    os.close(slave)
    transcript = bytearray()

    def until(marker):
        deadline = time.monotonic() + 15
        start = len(transcript)
        while marker not in transcript[start:]:
            remaining = deadline - time.monotonic()
            assert remaining > 0, transcript.decode(errors="replace")[-7000:]
            readable, _, _ = select.select([master], [], [], remaining)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    pytest.fail(transcript.decode(errors="replace")[-7000:])
                assert chunk, transcript.decode(errors="replace")[-7000:]
                transcript.extend(chunk)

    try:
        until(b"PTY_READY")
        os.write(master, b"typed-input\x1b[D\x1b[D")
        until(b"typed-input")
        # Probe acknowledgement is a barrier, not a timing sleep.
        os.kill(child.pid, signal.SIGUSR2)
        until(b"CURSOR 9")
        os.kill(child.pid, signal.SIGUSR1)
        until(b"RunId:")
        os.write(master, b"\x0f\x0f")  # detail view preserves the same buffer
        os.kill(child.pid, signal.SIGUSR2)
        until(b"CURSOR 9")
        os.write(master, b"\r")
        until(b"ACK:typed-input")
        os.write(master, b"/exit\r")
        until(b"PTY_EXIT 0")
        assert child.wait(timeout=5) == 0
        assert transcript.count(b"ACK:typed-input") == 1
        assert b"Traceback" not in transcript
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        os.close(master)


if __name__ == "__main__":
    asyncio.run(_fixture())
