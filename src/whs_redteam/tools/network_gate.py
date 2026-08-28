"""Single mandatory policy gateway for browser network actions."""

from __future__ import annotations

from whs_redteam.audit import AuditLogger
from whs_redteam.exceptions import (
    PolicyViolation,
    RequestLimitExceeded,
    RuntimeLimitExceeded,
)
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard


class NetworkGate:
    def __init__(
        self,
        scope_guard: ScopeGuard,
        budget: RequestBudget,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.scope_guard = scope_guard
        self.budget = budget
        self.audit_logger = audit_logger
        self.observed = 0
        self.blocked = 0
        self._fatal_error: RequestLimitExceeded | RuntimeLimitExceeded | None = None

    async def authorize(self, url: str, method: str) -> None:
        self.raise_if_halted()
        self.observed += 1
        try:
            self.scope_guard.authorize(url, method)
            await self.budget.acquire()
        except (RequestLimitExceeded, RuntimeLimitExceeded) as exc:
            self.blocked += 1
            self._fatal_error = exc
            if self.audit_logger is not None:
                self.audit_logger.emit(
                    "engagement_halted", url=url, method=method, reason=str(exc)
                )
            raise
        except PolicyViolation as exc:
            self.blocked += 1
            if self.audit_logger is not None:
                self.audit_logger.emit(
                    "network_request_blocked", url=url, method=method, reason=str(exc)
                )
            raise
        if self.audit_logger is not None:
            self.audit_logger.emit("network_request_authorized", url=url, method=method)

    def raise_if_halted(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error
