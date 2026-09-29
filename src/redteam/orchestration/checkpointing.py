"""LangGraph checkpointing.

One snapshot per super-step, enabling ``state_history`` and replay.

The allowlist is load-bearing: LangGraph only reconstructs types on an
explicit ``(module, name)`` allowlist and silently returns raw dicts for
anything else, so it is derived by walking our own packages rather than
typed out by hand. Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from contextlib import asynccontextmanager
from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

#: Packages whose types may legitimately appear inside ``GraphState``.
STATE_PACKAGES = ("redteam.core", "redteam.attack_graph", "redteam.orchestration")


def _fail_on_scan_error(package_name: str) -> None:
    """``pkgutil.walk_packages`` silently swallows an ``ImportError`` while
    scanning a package when no ``onerror`` is given -- meaning a broken
    submodule would silently *exclude* every state type it defines from
    the checkpoint allowlist, with no error at all (the exact "unknown
    type falls through silently" failure section 11 asks not to allow,
    just one step earlier: silently absent from the allowlist, rather
    than present but unrecognized). Re-raise instead of swallowing.
    """
    raise ImportError(f"checkpoint state scan could not import package {package_name!r}")


@lru_cache(maxsize=1)
def state_type_allowlist() -> tuple[tuple[str, str], ...]:
    """Every Pydantic model / Enum that can appear in ``GraphState``.

    Deliberately still discovered by walking ``STATE_PACKAGES`` rather
    than hand-typed as an explicit tuple: the alternative is a
    hand-maintained list of every ``BaseModel``/``Enum`` across three
    large packages (core, attack_graph, orchestration), which would
    silently go stale the next time someone adds a new state-carrying
    model and forgets to extend it -- a worse silent-failure mode than
    the one this reflection was meant to solve. What *is* made explicit,
    and was not before: a broken submodule (an ``ImportError`` while
    walking a package) now fails this call loudly instead of the walk
    silently pretending that submodule, and every state type it defines,
    does not exist.
    """
    found: set[tuple[str, str]] = set()
    for package_name in STATE_PACKAGES:
        package = importlib.import_module(package_name)
        modules = [package]
        for info in pkgutil.walk_packages(
            getattr(package, "__path__", []),
            prefix=f"{package_name}.",
            onerror=_fail_on_scan_error,
        ):
            modules.append(importlib.import_module(info.name))

        for module in modules:
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if not obj.__module__.startswith("redteam."):
                    continue
                if issubclass(obj, (BaseModel, Enum)):
                    found.add((obj.__module__, obj.__name__))
    return tuple(sorted(found))


def _serializer() -> Any:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=list(state_type_allowlist()))


def build_checkpointer(*, enabled: bool = True) -> Any | None:
    """An in-memory saver, or ``None`` when checkpointing is off.

    Durable runs use :func:`open_checkpointer` with the SQLite backend.
    """
    if not enabled:
        return None

    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver(serde=_serializer())


@asynccontextmanager
async def open_checkpointer(
    *, enabled: bool = True, backend: str = "memory", path: str | None = None
):
    """Open a checkpointer with explicit lifecycle ownership.

    SQLite's async saver owns a live connection, so callers must keep this
    context open for history/replay and close it when the runner is done.
    """

    if not enabled:
        yield None
        return
    if backend == "memory":
        yield build_checkpointer(enabled=True)
        return
    if backend != "sqlite":
        raise ValueError(f"unsupported checkpoint backend: {backend!r}")
    if not path:
        raise ValueError("sqlite checkpoint backend needs a path")

    from pathlib import Path

    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:  # pragma: no cover - optional-extra failure path
        raise RuntimeError(
            'SQLite checkpointing requires pip install -e ".[orchestration]"'
        ) from exc

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(resolved)
    try:
        yield AsyncSqliteSaver(connection, serde=_serializer())
    finally:
        await connection.close()
