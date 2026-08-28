"""Central error classification and bounded exponential retry policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx

from whs_redteam.exceptions import (
    CapabilityPolicyViolation,
    DnsPolicyViolation,
    ProbeRegistrationError,
    RateLimitExceeded,
    RequestLimitExceeded,
    RuntimeLimitExceeded,
    ScopeViolation,
)


class ErrorDisposition(StrEnum):
    RETRY = "RETRY"
    REJECT = "REJECT"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    disposition: ErrorDisposition
    delay_seconds: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.base_delay_seconds < 0:
            raise ValueError("base retry delay cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max retry delay cannot be below base delay")

    def classify(
        self,
        error: Exception,
        *,
        attempt_count: int,
        max_attempts: int,
        retry_after_seconds: float | None = None,
    ) -> RetryDecision:
        if isinstance(error, (RequestLimitExceeded, RuntimeLimitExceeded)):
            return RetryDecision(
                ErrorDisposition.TERMINAL,
                reason="agent run budget cannot recover within this run",
            )
        if isinstance(
            error,
            (
                CapabilityPolicyViolation,
                DnsPolicyViolation,
                ProbeRegistrationError,
                ScopeViolation,
            ),
        ):
            return RetryDecision(
                ErrorDisposition.REJECT,
                reason="policy or registration errors must not be retried",
            )
        retryable = isinstance(
            error,
            (
                TimeoutError,
                ConnectionError,
                RateLimitExceeded,
                httpx.TimeoutException,
                httpx.NetworkError,
            ),
        )
        if not retryable:
            return RetryDecision(
                ErrorDisposition.TERMINAL,
                reason="unknown errors fail closed",
            )
        if attempt_count >= max_attempts:
            return RetryDecision(
                ErrorDisposition.TERMINAL,
                reason="maximum retry attempts exhausted",
            )
        calculated = self.base_delay_seconds * (2 ** max(0, attempt_count - 1))
        if retry_after_seconds is not None:
            calculated = max(0.0, retry_after_seconds)
        return RetryDecision(
            ErrorDisposition.RETRY,
            delay_seconds=min(self.max_delay_seconds, calculated),
            reason="transient error with remaining attempts",
        )
