"""One in-process cooldown per LLM connection, not another retry loop."""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import Callable
from typing import Any

from redteam.core.ports.llm import LLMProviderFailure, ProviderRetryPolicy


class ProviderBackoff:
    def __init__(self, policy: ProviderRetryPolicy) -> None:
        self.policy = policy
        self._until = 0.0
        self._delay = policy.base_delay_seconds
        self._failure: LLMProviderFailure | None = None

    def failed(self, failure: LLMProviderFailure) -> None:
        if failure.kind == "quota":
            self._failure = failure
            return
        if self._failure is not None and self._failure.kind == "quota":
            return
        if not failure.retryable or failure.kind not in {"rate_limited", "server"}:
            return
        delay = failure.retry_after
        if delay is None:
            delay = self._delay
        # Jitter may increase the server floor; it must never lower it.
        delay += random.uniform(0, self.policy.base_delay_seconds / 4)
        self._until = max(self._until, time.monotonic() + delay)
        self._delay = min(self._delay * 2, self.policy.max_delay_seconds)
        self._failure = failure

    def succeeded(self) -> None:
        # A sibling that started before a 429 must not clear its cooldown.
        if time.monotonic() >= self._until and (
            self._failure is None or self._failure.kind != "quota"
        ):
            self._delay = self.policy.base_delay_seconds
            self._failure = None

    async def wait(
        self, *, max_wait_seconds: float | None = None,
        before_call: Callable[..., Any] | None = None,
    ) -> float:
        started = time.monotonic()
        limit = self.policy.max_wait_seconds
        if max_wait_seconds is not None:
            limit = min(limit, max(0.0, max_wait_seconds))
        while True:
            if before_call is not None:
                result = before_call()
                if inspect.isawaitable(result):
                    await result
            if self._failure is not None and self._failure.kind == "quota":
                raise self._failure
            now = time.monotonic()
            remaining = max(0.0, self._until - now)
            if remaining <= 0:
                return max(0.0, now - started)
            if remaining > max(0.0, limit - (now - started)):
                failure = self._failure
                raise LLMProviderFailure(
                    kind=failure.kind if failure else "rate_limited",
                    http_status=failure.http_status if failure else None,
                    provider_code=failure.provider_code if failure else "unknown",
                    retryable=True, retry_after=remaining,
                )
            # Keep cancellation, stop and deadline guards responsive while waiting.
            await asyncio.sleep(min(remaining, 0.5))
