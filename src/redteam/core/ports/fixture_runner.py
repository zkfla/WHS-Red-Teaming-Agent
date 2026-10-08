"""Network-free local fixture contract, deliberately not a target execution port."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FixtureCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input: Any
    expected: Any


class FixtureProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    source: str = Field(min_length=1, max_length=16384)
    cases: list[FixtureCase] = Field(min_length=1, max_length=16)


class FixturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # v2: host-side oracle; expected answers are not mounted in the sandbox.
    schema_version: Literal["2"] = "2"
    image: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
    timeout_seconds: float = Field(default=10, ge=1, le=30)
    max_output_bytes: int = Field(default=8192, ge=1024, le=65536)


class FixtureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["passed", "failed", "timed_out", "output_limit", "unavailable"]
    total: int = Field(ge=0, le=16)
    passed: int = Field(default=0, ge=0, le=16)
    # This contract carries no raw generated-code stdout or actual-target proof.
    fixture_only: Literal[True] = True

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.passed > self.total or (
            self.status == "passed" and (self.total == 0 or self.passed != self.total)
        ):
            raise ValueError("fixture_result_counts_inconsistent")
        return self


class FixtureRunner(Protocol):
    @property
    def policy(self) -> FixturePolicy: ...

    async def run(self, program: FixtureProgram) -> FixtureResult: ...
