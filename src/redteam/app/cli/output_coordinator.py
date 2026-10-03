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
        self.failure: BaseException | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._consume(), name="cli-output")

    def _fatal(self) -> BaseException | None:
        task = self.task
        if task is not None and task.done():
            if task.cancelled():
                return self.failure or RuntimeError("출력 작업이 취소되었습니다.")
            error = task.exception()
            if error is not None:
                return error
            return self.failure or RuntimeError("출력 작업이 종료되었습니다.")
        return self.failure

    def _fail(self, error: BaseException) -> None:
        if self.failure is None:
            self.failure = error

    def emit(self, content: Any, *, critical: bool = True) -> None:
        if self._fatal() is not None:
            return
        if isinstance(content, str):
            content = Text(safe_text(content))
        if self.queue.full():
            if not critical:
                self.coalesced += 1
                return
            self.overloaded = True
            try:
                self.frontend.emergency("출력 과부하: 신규 실행 접수를 중지했습니다.")
                self.frontend.emergency(safe_text(content))
            except Exception as exc:
                self._fail(RuntimeError(f"비상 출력 실패: {type(exc).__name__}"))
                if self.task is not None and not self.task.done():
                    self.task.cancel()
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
                    try:
                        self.frontend.emergency(f"화면 출력 실패: {type(exc).__name__}")
                        self.frontend.emergency(safe_text(getattr(item, "markup", item)))
                    except Exception as fallback:
                        error = RuntimeError(
                            f"화면 출력 실패({type(exc).__name__}) 및 비상 출력 실패"
                        )
                        self._fail(error)
                        raise error from fallback
            finally:
                self.queue.task_done()

    async def fence(self) -> None:
        error = self._fatal()
        if error is not None:
            raise RuntimeError(f"출력 작업이 정지되었습니다: {error}") from error
        if self.task is None:
            raise RuntimeError("출력 작업이 시작되지 않았습니다.")
        ack = asyncio.get_running_loop().create_future()

        async def _put() -> None:
            if self.coalesced:
                count, self.coalesced = self.coalesced, 0
                await self.queue.put(Text(f"진행 알림 {count}개 집계됨 (출력량 제한)"))
            await self.queue.put(ack)

        putter = asyncio.create_task(_put())
        try:
            while not ack.done():
                watched = {ack, self.task}
                if not putter.done():
                    watched.add(putter)
                await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if ack.done():
                    break
                if self.task.done():
                    error = self._fatal() or RuntimeError("출력 작업이 종료되었습니다.")
                    raise RuntimeError(f"출력 작업이 정지되었습니다: {error}") from error
                if putter.done() and not putter.cancelled():
                    exc = putter.exception()
                    if exc is not None:
                        raise RuntimeError(f"출력 적재 실패: {type(exc).__name__}") from exc
        finally:
            ack.cancel()
            putter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(ack, putter, return_exceptions=True)

    async def close(self) -> None:
        task = self.task
        if task is None:
            return
        if (
            self.failure is None
            and task.done()
            and not task.cancelled()
            and task.exception() is None
        ):
            return
        error: BaseException | None = None
        cancel_error: BaseException | None = None
        try:
            await asyncio.wait_for(self.fence(), 5)
        except asyncio.CancelledError as exc:
            cancel_error = exc
        except Exception as exc:
            if self.failure is not None:
                error = self.failure
            elif not (task.done() and task.cancelled()):
                error = exc
        if cancel_error is None and not task.done():
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)
            try:
                await asyncio.wait({task}, timeout=5)
            except asyncio.CancelledError as exc:
                cancel_error = exc
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=5)
            except asyncio.CancelledError as exc:
                cancel_error = cancel_error or exc
        if not task.done():
            error = error or RuntimeError("출력 작업이 종료되지 않았습니다.")
        if task.done() and not task.cancelled():
            task_error = task.exception()
            if task_error is not None:
                error = error or task_error
        if error is not None:
            self._fail(error)
        if cancel_error is not None:
            raise cancel_error
        if error is not None:
            raise RuntimeError(f"출력 정리 실패: {error}") from error
