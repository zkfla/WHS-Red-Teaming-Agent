"""Offline fixture validation, never target execution or completion proof."""

import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CodeFixtureCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=100)
    input: dict
    expected: dict


class CodeFixtureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    validation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(min_length=1, max_length=32768)
    cases: list[CodeFixtureCase] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_json(self):
        try:
            size = len(json.dumps(self.model_dump(), allow_nan=False).encode())
        except (ValueError, TypeError, RecursionError):
            raise ValueError("fixture_json_invalid") from None
        if size > 65536:
            raise ValueError("fixture_request_oversized")
        return self


class CodeFixtureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["passed", "failed", "held"]
    reason_code: str = Field(pattern=r"^fixture_[a-z0-9_]+$")
    cases_checked: int = Field(ge=0, le=16, strict=True)
    cases_passed: int = Field(ge=0, le=16, strict=True)
    cleanup_verified: bool = Field(strict=True)
    fixture_only: Literal[True] = True
    target_execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def checked_outcome(self):
        if self.cases_passed > self.cases_checked:
            raise ValueError("fixture_case_counts_invalid")
        if self.state == "passed" and (
            not self.cleanup_verified or not self.cases_checked
            or self.cases_checked != self.cases_passed
        ):
            raise ValueError("fixture_success_unproven")
        if not self.cleanup_verified and self.state != "held":
            raise ValueError("fixture_cleanup_unproven")
        return self


class CodeFixtureRunner(Protocol):
    @property
    def fingerprint(self) -> str:
        """Immutable mechanism + operator policy identity; no secret values."""
        ...

    async def run(self, request: CodeFixtureRequest) -> CodeFixtureResult:
        """Only supplied JSON fixtures, isolated, bounded, without network.

        No host fallback. Cancellation propagates after bounded teardown.
        Missing/unverified outcomes remain held; never a target observation.
        """
        ...
