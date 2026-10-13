"""Bounded, cancellation-safe SQLite work with connections owned by one thread."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class StorageRetiredError(RuntimeError):
    """An explicitly migrated source may still be inspected, but not executed."""


def retirement_path(path: str | Path) -> Path:
    source = Path(path).resolve()
    return source.with_name(f".{source.name}.central-migration.json")


def read_storage_retirement(path: str | Path) -> dict | None:
    marker = retirement_path(path)
    if os.name == "nt":
        # Windows Run ownership is supported, but source migration markers use
        # a POSIX private-inode contract. Ordinary unmarked stores can run;
        # marked sources stay fenced rather than weakening marker validation.
        try:
            marker.lstat()
        except FileNotFoundError:
            return None
        raise StorageRetiredError("migration_retirement_marker_unsupported_platform")
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 4096):
            raise StorageRetiredError("migration_retirement_marker_unsafe")
        value = json.load(handle)
    if (not isinstance(value, dict) or value.get("version") != 1
            or any(not isinstance(value.get(key), str) or not value[key]
                   for key in ("target", "migration_id", "source_hash"))):
        raise StorageRetiredError("migration_retirement_marker_unreadable")
    return value


def check_storage_writable(path: str | Path) -> None:
    if read_storage_retirement(path) is not None:
        raise StorageRetiredError("storage_retired_by_explicit_migration")


def retire_storage(path: str | Path, identity: dict) -> None:
    """Persist a destination fence after import commit, without altering SQLite.

    Caller holds original cohort ownership and has stopped shared writers.
    Incomplete fences refuse target resume until an idempotent retry completes
    them. Exact replays succeed; another destination cannot reset usage.
    """
    previous = read_storage_retirement(path)
    if previous is not None:
        if previous != identity:
            raise StorageRetiredError("migration_source_already_retired_elsewhere")
        return
    marker = retirement_path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=marker.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(identity, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, marker)
        except FileExistsError:
            if read_storage_retirement(path) != identity:
                raise StorageRetiredError("migration_source_already_retired_elsewhere") from None
    finally:
        temporary.unlink()
    directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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


def connect(path: str | Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        check_storage_writable(path)
    db = sqlite3.connect(path, timeout=1.0)
    db.execute("PRAGMA busy_timeout=1000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def transaction(
    path: str | Path, operation: Callable[[sqlite3.Connection], T], *, write: bool = False
) -> T:
    """Retry lock contention only; every failed transaction is rolled back."""
    for attempt in range(3):
        db = connect(path, write=write)
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
