"""WebFrontend only observes the controller; it never mutates or classifies."""

from __future__ import annotations

import asyncio
import io

from rich.markdown import Markdown
from rich.text import Text

from redteam.app.cli.conversation import ConversationDecision
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult
from redteam.app.web.events import EventLog
from redteam.app.web.frontend import WebFrontend, _decision_data


class Conversation:
    async def decide(self, text, **kwargs):
        return ConversationDecision(mode="answer", response="공개 답변")


def controller_for(frontend):
    from rich.console import Console

    async def runner(objective, controller):  # never used: only answers are asked
        return RunResult()

    return SessionController(
        console=Console(file=io.StringIO(), width=80, color_system=None),
        frontend=frontend,
        conversation=Conversation(),
        runner=runner,
        engagement_name="eng",
        scope=["localhost"],
    )


def kinds(log):
    return [item["kind"] for item in log.tail()]


def test_decision_projection_includes_bounded_redacted_goal_criteria():
    decision = ConversationDecision(
        mode="execute",
        response="새 조사를 시작합니다.",
        objective="등록된 응답을 평가합니다.",
        success_criteria=["token=private-value", "허용된 응답 결과를 요약합니다"],
    )

    projected = _decision_data(decision)

    assert projected is not None
    assert projected["objective"] == "등록된 응답을 평가합니다."
    assert projected["success_criteria"] == [
        "token=***REDACTED***",
        "허용된 응답 결과를 요약합니다",
    ]


def test_binding_publishes_the_initial_state_once():
    log = EventLog()
    frontend = WebFrontend(log)
    controller_for(frontend)
    assert kinds(log) == ["state"]
    frontend.invalidate()
    frontend.invalidate()
    assert kinds(log) == ["state"]  # unchanged state is not republished
    state = log.tail()[0]["data"]
    assert state["phase"] == "idle" and state["engagement"] == "eng"
    assert state["pending_approval"] is None and state["pending_approvals"] == []
    assert state["binding"] is None


def test_state_publishes_every_live_pending_approval():
    log = EventLog()
    frontend = WebFrontend(log)
    controller = controller_for(frontend)
    controller.add_pending_approval("APR-1", run_id="run-A")
    controller.add_pending_approval("APR-2", run_id="run-A")

    state = [item["data"] for item in log.tail() if item["kind"] == "state"][-1]
    assert state["pending_approval"] == "APR-1"
    assert state["pending_approvals"] == ["APR-1", "APR-2"]


def test_bind_calls_the_host_hook_with_the_controller():
    seen = []
    frontend = WebFrontend(EventLog(), on_bind=seen.append)
    controller = controller_for(frontend)
    assert seen == [controller]


async def test_console_output_becomes_plain_text_items_and_is_masked():
    log = EventLog()
    frontend = WebFrontend(log)
    controller_for(frontend)
    await frontend.write(Text("plain line"), None)
    await frontend.write(Markdown("**md** body"), None)
    await frontend.write(Text("password=hunter2-secret-value"), None)
    await frontend.write(Text("   "), None)
    lines = [item["data"]["text"] for item in log.tail() if item["kind"] == "console"]
    assert lines[0] == "plain line"
    assert lines[1] == "**md** body"
    assert "hunter2-secret-value" not in lines[2]
    assert len(lines) == 3  # blank output is skipped


def test_emergency_is_recorded_as_an_error_level_console_item():
    log = EventLog()
    frontend = WebFrontend(log)
    controller_for(frontend)
    frontend.emergency("화면 출력 실패")
    item = [i for i in log.tail() if i["kind"] == "console"][-1]
    assert item["data"] == {"text": "화면 출력 실패", "level": "error"}


async def test_receipt_changes_are_published_once_per_change():
    log = EventLog()
    frontend = WebFrontend(log)
    controller = controller_for(frontend)
    controller.submit("안녕")
    frontend.invalidate()
    frontend.invalidate()
    inputs = [i["data"] for i in log.tail() if i["kind"] == "input"]
    assert [i["state"] for i in inputs] == ["queued"]
    assert inputs[0]["text"] == "안녕" and inputs[0]["input_id"] == "input_1"
    # The controller changes the state directly for answered questions.
    receipt = controller.receipts["input_1"]
    receipt.state = "handled"
    receipt.decision = ConversationDecision(mode="answer", response="공개 답변")
    frontend.invalidate()
    inputs = [i["data"] for i in log.tail() if i["kind"] == "input"]
    assert [i["state"] for i in inputs] == ["queued", "handled"]
    assert inputs[-1]["decision"]["response"] == "공개 답변"


async def test_run_loop_polls_until_the_controller_is_closing():
    log = EventLog()
    frontend = WebFrontend(log)
    controller = controller_for(frontend)
    task = asyncio.create_task(frontend.run())
    controller.submit("질문")
    await asyncio.sleep(0.05)
    assert any(item["kind"] == "input" for item in log.tail())
    controller.request_close()
    await asyncio.wait_for(task, 1)


def test_closed_frontend_stops_publishing():
    log = EventLog()
    frontend = WebFrontend(log)
    controller = controller_for(frontend)
    frontend.close()
    before = log.last_seq
    controller.submit("늦은 입력")
    frontend.invalidate()
    assert log.last_seq == before


async def test_console_output_is_mirrored_to_the_launching_terminal_and_masked():
    mirror = io.StringIO()
    frontend = WebFrontend(EventLog(), mirror=mirror)
    controller_for(frontend)
    await frontend.write(Text("open the sign-in page and enter the code"), None)
    await frontend.write(Text("password=hunter2-secret-value"), None)
    frontend.emergency("화면 출력 실패")
    echoed = mirror.getvalue()
    assert "open the sign-in page and enter the code" in echoed
    assert "화면 출력 실패" in echoed
    assert "hunter2-secret-value" not in echoed


async def test_a_broken_mirror_is_dropped_without_stopping_the_session():
    class Broken:
        def write(self, _):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    log = EventLog()
    frontend = WebFrontend(log, mirror=Broken())
    controller_for(frontend)
    await frontend.write(Text("first"), None)
    await frontend.write(Text("second"), None)
    texts = [i["data"]["text"] for i in log.tail() if i["kind"] == "console"]
    assert texts == ["first", "second"]
