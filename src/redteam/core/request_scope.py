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
from typing import Literal


@dataclass(frozen=True)
class RequestScope:
    run_id: str
    owner: str
    task_id: str | None = None


RequestPurpose = Literal[
    "planning",
    "atomization",
    "worker_decision",
    "worker_external_tool_decision",
    "worker_local_read_decision",
    "worker_settlement",
    "worker_text_only_held",
    "review",
    "conversation",
    "other",
]
RequestAttemptKind = Literal[
    "normal", "format_repair", "semantic_repair", "transport_retry",
]


@dataclass
class RequestMetadata:
    """Code-owned request classification; never derived from model text."""

    purpose: RequestPurpose
    attempt_kind: RequestAttemptKind = "normal"
    # Mutated by the metered facade after durable reservation. The enclosing
    # caller keeps this object across asyncio.wait_for's child-task boundary.
    request_id: str | None = None


@dataclass(frozen=True)
class CompletedRequest:
    run_id: str
    request_id: str


_scope: ContextVar[RequestScope | None] = ContextVar("llm_request_scope", default=None)
_metadata: ContextVar[RequestMetadata | None] = ContextVar(
    "llm_request_metadata", default=None,
)
_completed: ContextVar[CompletedRequest | None] = ContextVar(
    "llm_completed_request", default=None,
)


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


@contextmanager
def llm_request_metadata(
    purpose: RequestPurpose, *, attempt_kind: RequestAttemptKind = "normal",
) -> Iterator[RequestMetadata]:
    """Attach safe, code-known semantics to exactly the nested provider call."""
    metadata = RequestMetadata(purpose, attempt_kind)
    token = _metadata.set(metadata)
    try:
        yield metadata
    finally:
        _metadata.reset(token)


def current_request_metadata() -> RequestMetadata | None:
    return _metadata.get()


def mark_completed_request(run_id: str, request_id: str) -> None:
    _completed.set(CompletedRequest(run_id, request_id))


def clear_completed_request() -> None:
    _completed.set(None)


def take_completed_request(run_id: str) -> str | None:
    completed = _completed.get()
    _completed.set(None)
    if completed is None or completed.run_id != run_id:
        return None
    return completed.request_id
