"""Single bounded display queue with an acknowledged terminal fence."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import Any

from rich.text import Text

from redteam.app.cli.display import safe_text


class OutputCoordinator:
    def __init__(self, console: Any, frontend: Any, *, capacity: int = 512) -> None:
        self.console = console
        self.frontend = frontend
        self.queue: asyncio.Queue = asyncio.Queue(capacity)
        self.history: deque[str] = deque(maxlen=300)
        self.task: asyncio.Task | None = None
        self.overloaded = False
        self.coalesced = 0

    def start(self) -> None:
        self.task = asyncio.create_task(self._consume(), name="cli-output")

    def emit(self, content: Any, *, critical: bool = True) -> None:
        if isinstance(content, str):
            content = Text(safe_text(content))
        if self.queue.full():
            if not critical:
                self.coalesced += 1
                return
            self.overloaded = True
            self.frontend.emergency("출력 과부하: 신규 실행 접수를 중지했습니다.")
            self.frontend.emergency(safe_text(content))
            return
        self.history.append(safe_text(getattr(content, "markup", content)))
        self.queue.put_nowait(content)

    async def _consume(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, asyncio.Future):
                    if not item.done():
                        item.set_result(None)
                    continue
                try:
                    await asyncio.wait_for(self.frontend.write(item, self.console), timeout=5)
                except Exception as exc:
                    self.frontend.emergency(f"화면 출력 실패: {type(exc).__name__}")
                    self.frontend.emergency(safe_text(getattr(item, "markup", item)))
            finally:
                self.queue.task_done()

    async def fence(self) -> None:
        if self.coalesced:
            count, self.coalesced = self.coalesced, 0
            await self.queue.put(Text(f"진행 알림 {count}개 집계됨 (출력량 제한)"))
        ack = asyncio.get_running_loop().create_future()
        await self.queue.put(ack)
        await ack

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.fence(), 5)
            await self.queue.put(None)
            if self.task:
                await asyncio.wait_for(self.task, 5)
        finally:
            if self.task and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
