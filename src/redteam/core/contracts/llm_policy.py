"""Explicit request-count policy; none of these settings grant tool authority."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMRequestPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    mode: Literal["bounded", "unlimited_requests"]
    max_requests: int | None = Field(default=None, strict=True, gt=0)
    max_concurrent_requests: int = Field(default=1, strict=True, gt=0)
    # After this many consecutive retryable 429s, an expired cooldown admits
    # one transactionally claimed recovery request. It is not a permanent Run
    # ban. Non-retryable or excessive Retry-After responses still require
    # operator review/new Run admission.
    max_consecutive_rate_limits: int = Field(default=3, strict=True, gt=0)
    rate_limit_base_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    max_rate_limit_wait_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _consistent(self) -> LLMRequestPolicy:
        if (self.mode == "bounded") != (self.max_requests is not None):
            raise ValueError("bounded requires max_requests; unlimited_requests requires null")
        if self.rate_limit_base_seconds > self.max_rate_limit_wait_seconds:
            raise ValueError("rate limit base delay exceeds maximum wait")
        return self
