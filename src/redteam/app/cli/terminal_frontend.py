"""An inline terminal application. Its buffer outlives every Run."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, TextIO, cast

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style

from redteam.app.cli.display import safe_text

PHASE_LABELS = {
    "idle": "입력 대기",
    "starting": "시작 중",
    "running": "조사 중",
    "waiting_approval": "승인 필요",
    "paused": "일시 중단",
    "settling": "결과 정리 중",
    "closing": "종료 중",
    "closed": "종료됨",
}


class TerminalFrontend:
    interactive = True

    def __init__(self, *, input=None, output=None) -> None:
        self.controller: Any = None
        self.draft_binding = None
        self.draft_approval = None
        self._draft_started = False
        self.details = False
        self._last_activity = ""
        self._activity_started = time.monotonic()
        self.started = asyncio.Event()
        self.buffer = Buffer(multiline=True, on_text_changed=self._edited)
        keys = KeyBindings()

        @keys.add("enter")
        def submit(event):
            if not self.buffer.text.strip():
                return
            if self.controller.submit(
                self.buffer.text, binding=self.draft_binding, approval_id=self.draft_approval
            ):
                self.buffer.append_to_history()
                self._reset_input()

        @keys.add("c-j")
        def newline(event):
            self.buffer.insert_text("\n")

        @keys.add("c-c")
        def clear(event):
            self._reset_input()

        @keys.add("c-d")
        def exit_session(event):
            if self.buffer.text:
                self.buffer.delete()
            else:
                self.controller.request_close()

        @keys.add("c-o")
        def details(event):
            self.details = not self.details
            event.app.invalidate()

        @keys.add("escape")
        def close_panel(event):
            self.details = False
            self.buffer.cancel_completion()
            event.app.invalidate()

        @keys.add("tab")
        def complete(event):
            if self.buffer.text.startswith("/"):
                if self.buffer.complete_state:
                    self.buffer.complete_next()
                else:
                    self.buffer.start_completion(select_first=False)

        body = HSplit(
            [
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._trace), height=Dimension(max=8), wrap_lines=True
                    ),
                    filter=Condition(lambda: self.details),
                ),
                Window(
                    FormattedTextControl(self._activity),
                    height=Dimension(min=1, max=6),
                    wrap_lines=True,
                ),
                Window(height=1, char="─", style="class:muted"),
                Window(
                    BufferControl(buffer=self.buffer),
                    height=Dimension(min=1, max=6),
                    wrap_lines=True,
                    get_line_prefix=lambda line, wrap: "  " if wrap else "❯ ",
                ),
                Window(height=1, char="─", style="class:muted"),
                Window(FormattedTextControl(self._status), height=1),
                Window(
                    FormattedTextControl(self._help),
                    height=1,
                    style="class:muted",
                ),
            ]
        )
        root = FloatContainer(
            body, floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=6))]
        )
        self.app: Application[Any] = Application(
            layout=Layout(root),
            key_bindings=keys,
            full_screen=False,
            input=input,
            output=output,
            mouse_support=False,
            refresh_interval=1,
            style=Style.from_dict({"muted": "ansibrightblack"}),
        )

    def bind(self, controller) -> None:
        self.controller = controller
        self.buffer.completer = WordCompleter(
            ["/" + name for name in controller.commands],
            meta_dict={"/" + name: desc for name, desc in controller.command_help.items()},
        )

    def _help(self) -> str:
        if self.controller and self.controller.pending_approval:
            return "approve <ID> / deny <ID> · Ctrl+J 줄바꿈 · /stop 중단"
        if self.controller and self.controller.phase.value == "paused":
            return "/resume 재개 · /stop 중단 · Ctrl+O 상세"
        return "/ + Tab 명령 · Ctrl+J 줄바꿈 · Ctrl+O 상세 · /stop 중단"

    def _reset_input(self) -> None:
        self.buffer.reset()
        self._draft_started = False
        self.draft_binding = self.draft_approval = None

    def _edited(self, buffer) -> None:
        if not buffer.text:
            self._draft_started = False
            self.draft_binding = self.draft_approval = None
        elif not self._draft_started and self.controller is not None:
            self._draft_started = True
            self.draft_binding = self.controller.input_binding
            self.draft_approval = self.controller.pending_approval

    def _activity(self) -> str:
        activity = self.controller.activity if self.controller else "입력 준비 중"
        if activity != self._last_activity:
            self._last_activity = activity
            self._activity_started = time.monotonic()
        if self.controller and self.controller.phase.value in {"starting", "running"}:
            activity += f" · {int(time.monotonic() - self._activity_started)}초"
        return safe_text(activity)

    def _status(self) -> str:
        if self.controller is None:
            return "입력 준비 중"
        c = self.controller
        bound = self.draft_binding if self._draft_started else c.input_binding
        target = (bound.run_id or bound.start_request_id) if bound else "새 문맥"
        return safe_text(
            f"{PHASE_LABELS[c.phase.value]} · 이 입력: {target} · 보류 {c.pending_count}"
        )

    def _trace(self) -> str:
        if not self.controller:
            return ""
        return "최근 상세 (최대 300항목 보존; /trace로 조회)\n" + "\n".join(
            list(self.controller.output.history)[-8:]
        )

    def invalidate(self) -> None:
        self.app.invalidate()

    async def run(self) -> None:
        try:
            await self.app.run_async(pre_run=self.started.set)
        finally:
            self.controller.request_close()

    def close(self) -> None:
        if self.app.is_running and not self.app.is_done:
            self.app.exit()

    async def write(self, content, console) -> None:
        await self.started.wait()
        if self.app.is_running:
            # Display only: never enter cooked mode or detach stdin. The generic
            # run_in_terminal helper does both, so an Enter arriving during a
            # print can become Ctrl+J and silently insert a newline instead.
            # No await between erase and invalidate: the renderer and this
            # single writer run on the same event loop.
            self.app.renderer.erase()
            try:
                console.print(content)
            finally:
                self.app.renderer.reset()
                self.app.invalidate()
        else:
            console.print(content)

    def emergency(self, text: str) -> None:
        # Last-resort failure path only, never execute control sequences.
        cast(TextIO, sys.__stderr__).write(safe_text(text) + "\n")


class ConsoleFrontend:
    """Sequential non-TTY/setup compatibility; never claim live steering."""

    interactive = False

    def __init__(self, console) -> None:
        self.console = console
        self.controller: Any = None
        self.details = False

    def bind(self, controller) -> None:
        self.controller = controller

    def invalidate(self) -> None:
        pass

    async def run(self) -> None:
        while not self.controller.closing.is_set():
            await self.controller.idle.wait()
            await self.controller.output.fence()
            try:
                text = await self.read_line()
            except EOFError:
                self.controller.output.emit("Session ended. 세션 종료 요청됨.")
                self.controller.request_close()
                return
            except KeyboardInterrupt:
                continue
            self.controller.submit(text)
            # This frontend deliberately reads the next line after processing.
            await self.controller.join_inputs()

    async def read_line(self) -> str:
        return self.console.input("\n[cyan]❯[/cyan] ")

    def close(self) -> None:
        pass

    async def write(self, content, console) -> None:
        console.print(content)

    def emergency(self, text: str) -> None:
        self.console.print(safe_text(text))


class PlainFrontend(ConsoleFrontend):
    """Opt-in static transcript; sequential input, no live steering promises."""

    def __init__(self, console) -> None:
        super().__init__(console)
        self.prompt: PromptSession[str] = PromptSession()

    async def read_line(self) -> str:
        return await self.prompt.prompt_async(
            "operator> ", enable_history_search=False, complete_while_typing=False
        )

    async def write(self, content, console) -> None:
        lines = console.render_lines(content, console.options, pad=False)
        text = "\n".join("".join(segment.text for segment in line) for line in lines)
        text = safe_text(text).translate(
            str.maketrans(
                cast(
                    dict[str | int, str | int | None],
                    {
                        "❯": ">",
                        "✓": "+",
                        "✗": "x",
                        "●": "*",
                        "○": "o",
                        "→": ">",
                        "─": "-",
                    },
                )
            )
        )
        console.print(text, markup=False, highlight=False, style="default")
