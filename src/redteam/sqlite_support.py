"""Bounded, cancellation-safe SQLite work with connections owned by one thread."""

from __future__ import annotations

import asyncio
import contextvars
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def _lock_contention(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {5, 6}  # SQLITE_BUSY / SQLITE_LOCKED, including extended codes
    # Python 3.10 does not expose sqlite_errorcode.
    return str(exc).split(":", 1)[0] in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=1.0)
    db.execute("PRAGMA busy_timeout=1000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def transaction(
    path: str | Path, operation: Callable[[sqlite3.Connection], T], *, write: bool = False
) -> T:
    """Retry lock contention only; every failed transaction is rolled back."""
    for attempt in range(3):
        db = connect(path)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = operation(db)
            db.commit()
            return result
        except sqlite3.OperationalError as exc:
            db.rollback()
            if not _lock_contention(exc) or attempt == 2:
                raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    raise RuntimeError("SQLite retry limit exhausted")  # defensive; the last attempt raises


async def finish_thread(operation: Callable[[], T]) -> T:
    """Cancellation waits for this same worker's commit/rollback, never reruns it."""
    context = contextvars.copy_context()
    # Use an executor Future, not another asyncio Task: shutdown may cancel every
    # Task while the underlying thread is still committing.
    pending = asyncio.get_running_loop().run_in_executor(None, context.run, operation)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            if pending.done():
                # A callback may itself raise CancelledError after rolling back.
                # Observe its result and propagate, instead of retrying a finished Future.
                try:
                    pending.result()
                except BaseException:
                    pass
                raise
            cancelled = exc
            continue
        except BaseException:
            if cancelled is not None:
                raise cancelled from None
            raise
        else:
            if cancelled is not None:
                raise cancelled
            return result


async def run_transaction(
    path: str | Path, operation: Callable[[sqlite3.Connection], T], *, write: bool = False
) -> T:
    return await finish_thread(lambda: transaction(path, operation, write=write))


async def run_sqlite(operation: Callable[[], T]) -> T:
    """Run a callback that owns and closes its transaction, with at most three attempts.

    The callback must roll back before raising. Do not compose with ``transaction``
    or ``run_transaction``: each already provides its own bounded retry loop.
    """

    def retry() -> T:
        for attempt in range(3):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not _lock_contention(exc) or attempt == 2:
                    raise
        raise RuntimeError("SQLite retry limit exhausted")

    return await finish_thread(retry)
