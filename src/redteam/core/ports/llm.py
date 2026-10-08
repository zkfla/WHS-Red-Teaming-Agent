"""LLM Gateway port. Concrete implementations in ``redteam.llm``.

Keeping LiteLLM (or any provider SDK) call sites behind this Protocol is
what lets the model/provider be swapped without touching Agent code.

Provider failures cross this boundary only as :class:`LLMProviderFailure`:
kind, HTTP status, short provider code, retryable flag, and resume delay.
Unknown values stay ``unknown``/``None``. Full bodies, headers, and secrets
never cross here. Central callers read these fields; they never branch on
SDK exception types.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderRetryPolicy(BaseModel):
    """Bounded provider waiting; actual request limits remain with the caller."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    base_delay_seconds: float = Field(default=1.0, gt=0)
    max_delay_seconds: float = Field(default=30.0, gt=0)
    max_wait_seconds: float = Field(default=30.0, ge=0)

    @model_validator(mode="after")
    def validate_delay_range(self) -> ProviderRetryPolicy:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        return self


@dataclass(frozen=True)
class StructuredCompletion:
    """Request-local model value and measured total tokens, when the provider reports it."""

    #: Raw text is allowed so format errors retain their measured request cost.
    value: dict[str, Any] | str
    total_tokens: int | None = None


_PROVIDER_KINDS = frozenset(
    {
        "rate_limited",
        "quota",
        "auth",
        "invalid_request",
        "server",
        "network",
        "timeout",
        "unknown",
    }
)


def _clean_kind(value: Any) -> str:
    if isinstance(value, str) and value in _PROVIDER_KINDS:
        return value
    return "unknown"


def _clean_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _clean_provider_code(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()[:100]
    if not text:
        return "unknown"
    if all(char.isalnum() or char in "_-." for char in text):
        return text
    return "unknown"


def _clean_retry_after(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number == float("inf") or number == float("-inf"):
            return None
        if number < 0:
            return None
        return number
    return None


class LLMProviderFailure(Exception):
    """Minimal public contract for a failed provider request.

    Adapters raise this instead of leaking SDK exceptions. Central code
    reads the fields without importing any SDK type. ``message`` is a
    fixed safe phrase plus status/code only; bodies, headers, prompts,
    and secrets are never stored here.
    """

    def __init__(
        self,
        *,
        kind: str = "unknown",
        http_status: int | None = None,
        provider_code: str = "unknown",
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.kind = _clean_kind(kind)
        self.http_status = _clean_http_status(http_status)
        self.provider_code = _clean_provider_code(provider_code)
        self.retryable = bool(retryable)
        self.retry_after = _clean_retry_after(retry_after)
        status = str(self.http_status) if self.http_status is not None else "unknown"
        super().__init__(
            f"provider request failed (kind={self.kind}, "
            f"status={status}, code={self.provider_code})"
        )

    @property
    def provider_status(self) -> int | None:
        """Alias matching the diagnostics field name."""
        return self.http_status

    def diagnostic_fields(self) -> dict[str, Any]:
        """Public payload for the diagnostics owner to copy verbatim."""
        return {
            "provider_status": self.http_status,
            "provider_code": self.provider_code,
            "retry_after": self.retry_after,
        }


@runtime_checkable
class MeteredLLMClient(Protocol):
    """Optional telemetry path; existing JSON-only providers remain compatible."""

    async def complete_structured_with_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> StructuredCompletion: ...


@runtime_checkable
class ProviderBackoffClient(Protocol):
    """Optional shared connection readiness, checked before request accounting.

    Waiting does not make a model request. Guards remain owned by the caller.
    Implementations return elapsed waiting and raise a safe provider failure
    when the delay cannot fit; they must never shorten the server's delay.
    """

    @property
    def provider_wait_limit_seconds(self) -> float: ...

    async def wait_for_provider(
        self, *, max_wait_seconds: float | None = None,
        before_call: Callable[..., Any] | None = None,
    ) -> float: ...


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Free-text completion."""
        ...

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Completion constrained to a JSON schema."""
        ...
