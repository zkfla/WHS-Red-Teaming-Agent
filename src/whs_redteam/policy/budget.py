"""Request count, runtime, and rate budgets."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from time import time as wall_time
from typing import TYPE_CHECKING

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import (
    RateLimitExceeded,
    RequestLimitExceeded,
    RuntimeLimitExceeded,
)

if TYPE_CHECKING:
    from whs_redteam.inventory import InventoryStore


@dataclass(slots=True)
class RequestBudget:
    """Fail closed when an engagement exceeds configured execution budgets."""

    max_requests: int
    requests_per_second: int
    max_runtime_seconds: float
    audit_logger: AuditLogger | None = None
    clock: Callable[[], float] = monotonic
    _started_at: float = field(init=False)
    _request_count: int = field(default=0, init=False)
    _recent_requests: deque[float] = field(default_factory=deque, init=False)
    _async_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    @classmethod
    def from_config(
        cls,
        config: EngagementConfig,
        audit_logger: AuditLogger | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> RequestBudget:
        return cls(
            max_requests=config.max_requests,
            requests_per_second=config.requests_per_second,
            max_runtime_seconds=config.max_runtime_minutes * 60,
            audit_logger=audit_logger,
            clock=clock,
        )

    @property
    def request_count(self) -> int:
        return self._request_count

    def consume(self) -> None:
        """Synchronous fail-fast budget check used by deterministic unit callers."""

        now = self.clock()
        elapsed = now - self._started_at
        if elapsed >= self.max_runtime_seconds:
            self._deny("runtime_limit_exceeded", elapsed=elapsed)
            raise RuntimeLimitExceeded("maximum engagement runtime exceeded")
        if self._request_count >= self.max_requests:
            self._deny("request_limit_exceeded", request_count=self._request_count)
            raise RequestLimitExceeded("maximum request count exceeded")

        window_start = now - 1.0
        while self._recent_requests and self._recent_requests[0] <= window_start:
            self._recent_requests.popleft()
        if len(self._recent_requests) >= self.requests_per_second:
            self._deny("rate_limit_exceeded", recent_requests=len(self._recent_requests))
            raise RateLimitExceeded("requests-per-second limit exceeded")

        self._recent_requests.append(now)
        self._request_count += 1
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "request_budget_consumed",
                request_count=self._request_count,
                max_requests=self.max_requests,
            )

    async def acquire(
        self,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wait for rate capacity, but fail closed on total count or runtime limits."""

        while True:
            delay = 0.0
            async with self._async_lock:
                now = self.clock()
                elapsed = now - self._started_at
                if elapsed >= self.max_runtime_seconds:
                    self._deny("runtime_limit_exceeded", elapsed=elapsed)
                    raise RuntimeLimitExceeded("maximum engagement runtime exceeded")
                if self._request_count >= self.max_requests:
                    self._deny("request_limit_exceeded", request_count=self._request_count)
                    raise RequestLimitExceeded("maximum request count exceeded")

                window_start = now - 1.0
                while self._recent_requests and self._recent_requests[0] <= window_start:
                    self._recent_requests.popleft()
                if len(self._recent_requests) < self.requests_per_second:
                    self._recent_requests.append(now)
                    self._request_count += 1
                    if self.audit_logger is not None:
                        self.audit_logger.emit(
                            "request_budget_consumed",
                            request_count=self._request_count,
                            max_requests=self.max_requests,
                        )
                    return
                delay = max(0.001, self._recent_requests[0] + 1.0 - now)
                if elapsed + delay >= self.max_runtime_seconds:
                    self._deny("runtime_limit_exceeded", elapsed=elapsed + delay)
                    raise RuntimeLimitExceeded("maximum engagement runtime exceeded")
            await sleep(delay)

    def _deny(self, event_type: str, **fields: float | int) -> None:
        if self.audit_logger is not None:
            self.audit_logger.emit(event_type, **fields)


@dataclass(slots=True)
class PersistentRequestBudget:
    """One SQLite-backed request and runtime budget shared by every run stage."""

    run_id: str
    max_requests: int
    requests_per_second: int
    max_runtime_seconds: float
    inventory: InventoryStore
    audit_logger: AuditLogger | None = None
    clock: Callable[[], float] = wall_time
    _async_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        now = self.clock()
        self.inventory.connection.execute(
            """
            INSERT OR IGNORE INTO run_budgets (
                run_id, max_requests, used_requests, requests_per_second,
                started_at, deadline, recent_requests, paused_at
            ) VALUES (?, ?, 0, ?, ?, ?, '[]', NULL)
            """,
            (
                self.run_id,
                self.max_requests,
                self.requests_per_second,
                now,
                now + self.max_runtime_seconds,
            ),
        )
        self.inventory.connection.commit()
        row = self._row()
        if (
            int(row["max_requests"]) != self.max_requests
            or int(row["requests_per_second"]) != self.requests_per_second
        ):
            raise ValueError("persisted run budget does not match engagement configuration")

    @classmethod
    def from_config(
        cls,
        run_id: str,
        config: EngagementConfig,
        inventory: InventoryStore,
        audit_logger: AuditLogger | None = None,
        clock: Callable[[], float] = wall_time,
    ) -> PersistentRequestBudget:
        return cls(
            run_id=run_id,
            max_requests=config.max_requests,
            requests_per_second=config.requests_per_second,
            max_runtime_seconds=config.max_runtime_minutes * 60,
            inventory=inventory,
            audit_logger=audit_logger,
            clock=clock,
        )

    @property
    def request_count(self) -> int:
        return int(self._row()["used_requests"])

    def consume(self) -> None:
        delay = self._try_consume()
        if delay > 0:
            self._deny("rate_limit_exceeded", recent_requests=self.requests_per_second)
            raise RateLimitExceeded("requests-per-second limit exceeded")

    def pause(self) -> None:
        now = self.clock()
        self.inventory.connection.execute(
            """
            UPDATE run_budgets SET paused_at = COALESCE(paused_at, ?)
            WHERE run_id = ?
            """,
            (now, self.run_id),
        )
        self.inventory.connection.commit()

    def resume(self) -> None:
        now = self.clock()
        connection = self.inventory.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row()
            paused_at = row["paused_at"]
            if paused_at is not None:
                extension = max(0.0, now - float(paused_at))
                connection.execute(
                    """
                    UPDATE run_budgets SET deadline = deadline + ?,
                        paused_at = NULL, recent_requests = '[]'
                    WHERE run_id = ?
                    """,
                    (extension, self.run_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def acquire(
        self,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        while True:
            async with self._async_lock:
                delay = self._try_consume()
            if delay == 0:
                return
            await sleep(delay)

    def _try_consume(self) -> float:
        now = self.clock()
        connection = self.inventory.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._row()
            if row["paused_at"] is not None:
                raise RuntimeLimitExceeded("agent run budget is paused")
            if now >= float(row["deadline"]):
                raise RuntimeLimitExceeded("maximum agent run runtime exceeded")
            used = int(row["used_requests"])
            if used >= self.max_requests:
                raise RequestLimitExceeded("maximum agent run request count exceeded")
            recent = [
                float(value)
                for value in json.loads(row["recent_requests"])
                if float(value) > now - 1.0
            ]
            if len(recent) >= self.requests_per_second:
                delay = max(0.001, recent[0] + 1.0 - now)
                if now + delay >= float(row["deadline"]):
                    raise RuntimeLimitExceeded("maximum agent run runtime exceeded")
                connection.execute(
                    "UPDATE run_budgets SET recent_requests = ? WHERE run_id = ?",
                    (json.dumps(recent), self.run_id),
                )
                connection.commit()
                return delay
            recent.append(now)
            connection.execute(
                """
                UPDATE run_budgets SET used_requests = used_requests + 1,
                    recent_requests = ? WHERE run_id = ?
                """,
                (json.dumps(recent), self.run_id),
            )
            connection.commit()
        except (RequestLimitExceeded, RuntimeLimitExceeded) as exc:
            connection.rollback()
            event = (
                "request_limit_exceeded"
                if isinstance(exc, RequestLimitExceeded)
                else "runtime_limit_exceeded"
            )
            self._deny(event, request_count=self.request_count)
            raise
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "request_budget_consumed",
                run_id=self.run_id,
                request_count=used + 1,
                max_requests=self.max_requests,
            )
        return 0.0

    def _row(self):
        row = self.inventory.connection.execute(
            "SELECT * FROM run_budgets WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"run budget not found: {self.run_id}")
        return row

    def _deny(self, event_type: str, **fields: float | int) -> None:
        if self.audit_logger is not None:
            self.audit_logger.emit(event_type, run_id=self.run_id, **fields)
