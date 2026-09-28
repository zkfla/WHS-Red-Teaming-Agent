"""Per-run orchestration limits.

Agent-Team-owned runtime shape; ``from_config`` bridges from the frozen
``core.config.OrchestrationConfig``. Execution policy only — nothing here
can widen what the Control Plane allows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from redteam.core.config import OrchestrationConfig

#: Sequential path has 7 nodes; fan-out adds WORKER + AGGREGATE.
NODES_PER_TURN = 9

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
    #: Stop repeatable or cycling plans that produce no meaningful graph delta.
    max_no_progress_turns: int = Field(default=3, ge=1)

    observation_max_per_type: int = Field(default=20, ge=1)
    observation_max_total: int = Field(default=80, ge=1)

    #: LangGraph checkpointing: keeps a snapshot per super-step so a run
    #: can be inspected and replayed from an earlier branch.
    checkpointing: bool = True
    checkpoint_backend: Literal["memory", "sqlite"] = "memory"
    checkpoint_path: str = "data/orchestration/checkpoints.sqlite3"
    thread_id: str | None = None

    work_backend: Literal["memory", "sqlite"] = "memory"
    work_path: str = "data/orchestration/work.sqlite3"

    global_concurrency: int = Field(default=4, ge=1)
    #: Number of independent candidates admitted from one planning frontier.
    #: One preserves the original sequential graph; values above one enable
    #: the LangGraph ``Send`` map/reduce path.
    max_actions_per_turn: int = Field(default=1, ge=1)
    per_target_concurrency: int = Field(default=1, ge=1)
    lease_ttl_seconds: float = Field(default=60.0, gt=0)
    drain_timeout_seconds: float = Field(default=30.0, gt=0)
    verification_reserve_ratio: float = Field(default=0.2, ge=0.0, le=0.9)
    branch_prune_failure_threshold: int = Field(default=2, ge=1)
    branch_prune_difficulty_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    branch_prune_objective_override: float = Field(default=0.8, ge=0.0, le=1.0)

    #: Hybrid planning: a single candidate, or a top-1 score lead of at least
    #: this much, is chosen deterministically and costs no LLM call. Anything
    #: closer is genuinely ambiguous and goes to a bounded rerank.
    planner_score_margin: float = Field(default=0.15, ge=0.0, le=10.0)
    #: How many of the top candidates a rerank is allowed to consider. The
    #: planner can only ever return one of these.
    planner_rerank_top_k: int = Field(default=3, ge=2, le=20)

    #: RC#1: when on (and an LLM client is wired), the model may *generate*
    #: candidate drafts each turn in addition to the playbook frontier. Every
    #: generated draft is compiled and policy-checked exactly like a playbook
    #: draft, so this only widens what is proposed, never what may run.
    #: Default off: the playbook-only frontier stays the baseline behaviour.
    llm_candidate_generation: bool = Field(default=False)

    #: Overrides the value derived from ``max_steps`` when set.
    recursion_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _checkpoint_path_is_safe(self) -> OrchestrationSettings:
        if self.checkpoint_backend == "sqlite" and not self.checkpoint_path.strip():
            raise ValueError("sqlite checkpointing needs a non-blank checkpoint_path")
        if self.work_backend == "sqlite" and not self.work_path.strip():
            raise ValueError("sqlite work backend needs a non-blank work_path")
        return self

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
            base["llm_candidate_generation"] = config.llm_candidate_generation
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(base)
