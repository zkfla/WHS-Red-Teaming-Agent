"""Input ownership across accepted submissions, cancellation and terminal output."""

import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from redteam.app.cli.session_types import BindingRef
from redteam.app.cli.terminal_frontend import TerminalFrontend


def frontend():
    submitted = []
    controller = SimpleNamespace(
        input_binding=BindingRef("request-a", "attachment-a", "run-a"),
        pending_approval="approval-a",
        commands=[],
        command_help={},
        accept=True,
    )

    def submit(text, **kwargs):
        submitted.append((text, kwargs))
        return controller.accept

    controller.submit = submit
    terminal = TerminalFrontend(input=DummyInput(), output=DummyOutput())
    terminal.bind(controller)
    return terminal, controller, submitted


def press(terminal, key):
    binding = terminal.app.key_bindings.get_bindings_for_keys((key,))[-1]
    binding.handler(None)


@pytest.mark.parametrize("key", [Keys.ControlM, Keys.ControlC])
def test_new_draft_uses_new_run_and_approval_after_submit_or_cancel(key):
    terminal, controller, submitted = frontend()
    previous = controller.input_binding
    terminal.buffer.insert_text("approve approval-a")
    press(terminal, key)

    assert terminal.buffer.text == ""
    if key == Keys.ControlM:
        assert submitted == [
            (
                "approve approval-a",
                {"binding": previous, "approval_id": "approval-a"},
            )
        ]
        assert list(terminal.buffer.history.get_strings()) == ["approve approval-a"]
    else:
        assert submitted == []

    current = BindingRef("request-b", "attachment-b", "run-b")
    controller.input_binding = current
    controller.pending_approval = "approval-b"
    terminal.buffer.insert_text("approve approval-b")
    press(terminal, Keys.ControlM)
    assert submitted[-1] == (
        "approve approval-b",
        {"binding": current, "approval_id": "approval-b"},
    )


def test_rejected_submission_keeps_draft_cursor_and_original_ownership():
    terminal, controller, submitted = frontend()
    previous = controller.input_binding
    terminal.buffer.insert_text("focus web")
    terminal.buffer.cursor_position = 3
    controller.input_binding = BindingRef("request-b", "attachment-b", "run-b")
    controller.pending_approval = "approval-b"
    controller.accept = False

    press(terminal, Keys.ControlM)

    assert terminal.buffer.text == "focus web"
    assert terminal.buffer.cursor_position == 3
    assert terminal.draft_binding == previous
    assert terminal.draft_approval == "approval-a"
    assert submitted[-1][1] == {"binding": previous, "approval_id": "approval-a"}
    assert list(terminal.buffer.history.get_strings()) == []


async def test_terminal_result_does_not_rebind_an_unsubmitted_draft():
    terminal, controller, submitted = frontend()
    previous = controller.input_binding
    terminal.buffer.insert_text("focus web")
    terminal.buffer.cursor_position = 3
    controller.input_binding = BindingRef("request-b", "attachment-b", "run-b")
    controller.pending_approval = "approval-b"
    output = io.StringIO()
    terminal.started.set()

    await terminal.write("Run finished", Console(file=output))

    assert "Run finished" in output.getvalue()
    assert terminal.buffer.text == "focus web"
    assert terminal.buffer.cursor_position == 3
    press(terminal, Keys.ControlM)
    assert submitted[-1][1] == {"binding": previous, "approval_id": "approval-a"}
