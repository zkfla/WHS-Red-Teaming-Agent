"""Per-run orchestration limits.

Agent-Team-owned runtime shape; ``from_config`` bridges from the frozen
``core.config.OrchestrationConfig``. Execution policy only — nothing here
can widen what the Control Plane allows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from redteam.core.config import OrchestrationConfig

#: OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN.
NODES_PER_TURN = 7

#: Head-room for the non-looping edges around the loop itself.
RECURSION_SLACK = 10


class OrchestrationSettings(BaseModel):
    """How a single ``AgentLoopRunner`` behaves."""

    #: Hard ceiling on OBSERVE->REPLAN turns, independent of the
    #: Control Plane's ``ExecutionBudget``. Both must hold.
    max_steps: int = Field(default=8, ge=1)
    max_retries: int = Field(default=3, ge=0)
    #: Stop after this many tool executions fail back to back — a target
    #: that keeps erroring should not be hammered for the whole budget.
    max_consecutive_failures: int = Field(default=3, ge=1)

    observation_max_per_type: int = Field(default=20, ge=1)
    observation_max_total: int = Field(default=80, ge=1)

    #: LangGraph checkpointing: keeps a snapshot per super-step so a run
    #: can be inspected and replayed from an earlier branch.
    checkpointing: bool = True
    thread_id: str | None = None

    #: Overrides the value derived from ``max_steps`` when set.
    recursion_limit: int | None = Field(default=None, ge=1)

    @property
    def effective_recursion_limit(self) -> int:
        if self.recursion_limit is not None:
            return self.recursion_limit
        return self.max_steps * NODES_PER_TURN + RECURSION_SLACK

    @classmethod
    def from_config(
        cls, config: OrchestrationConfig | None = None, **overrides: object
    ) -> OrchestrationSettings:
        """Read the shared ``AppConfig`` section, then apply overrides."""
        base: dict[str, object] = {}
        if config is not None:
            base["max_steps"] = config.max_steps
            base["max_retries"] = config.max_retries
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(base)
