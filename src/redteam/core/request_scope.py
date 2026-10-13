"""Run-scoped LLM request identity (core-owned).

Single authoritative request-scope ContextVar shared by orchestration
metering and provider adapters. Provider code under redteam.llm imports
from here so it never depends on orchestration wiring.
redteam.orchestration.metered_llm re-exports the same objects for
existing callers; no second ContextVar may be defined elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestScope:
    run_id: str
    owner: str
    task_id: str | None = None


_scope: ContextVar[RequestScope | None] = ContextVar("llm_request_scope", default=None)


@contextmanager
def llm_request_scope(run_id: str, owner: str, *, task_id: str | None = None) -> Iterator[None]:
    parent = _scope.get()
    if task_id is None and parent is not None and parent.run_id == run_id:
        task_id = parent.task_id
    token = _scope.set(RequestScope(run_id, owner, task_id))
    try:
        yield
    finally:
        _scope.reset(token)


def current_request_scope() -> RequestScope | None:
    """Read-only lifecycle identity for coordinated writes; never prompt data."""
    return _scope.get()
