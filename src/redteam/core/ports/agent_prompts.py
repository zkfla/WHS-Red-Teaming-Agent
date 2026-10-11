"""Trusted role instructions, independent of provider and untrusted context."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class PromptBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role_id: str
    version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentPrompts(Protocol):
    def binding(self, stage: str, *, role_id: str | None = None) -> PromptBinding: ...

    def compose(
        self, stage: str, runtime_contract: str, *, role_id: str | None = None,
    ) -> str: ...
