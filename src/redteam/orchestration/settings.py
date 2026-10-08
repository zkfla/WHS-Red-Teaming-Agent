"""Per-run orchestration limits.

Agent-Team-owned runtime shape; ``from_config`` bridges from the frozen
``core.config.OrchestrationConfig``. Execution policy only — nothing here
can widen what the Control Plane allows.
"""

from __future__ import annotations

import math
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
    #: Additional tool-execution attempts after a retryable failure. Only
    #: ever applied to a *known low-risk* (passive/read-only) tool so a
    #: bounded retry cannot silently repeat a state-changing side effect;
    #: see ``execute_node``. Set ``retry_retryable_tool_failures=False`` to
    #: disable retries entirely.
    max_retries: int = Field(default=3, ge=0)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    retry_retryable_tool_failures: bool = Field(default=True)
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
    task_graph_backend: Literal["memory", "sqlite"] = "memory"
    task_graph_path: str = "data/orchestration/task_graph.sqlite3"
    attack_graph_backend: Literal["memory", "sqlite"] = "memory"
    attack_graph_path: str = "data/orchestration/attack_graph.sqlite3"
    operator_intent_backend: Literal["memory", "sqlite"] = "memory"
    operator_intent_path: str = "data/orchestration/operator_intents.sqlite3"

    global_concurrency: int = Field(default=4, ge=1)
    #: Number of independent candidates admitted from one planning frontier.
    #: One preserves the original sequential graph; values above one enable
    #: the LangGraph ``Send`` map/reduce path.
    max_actions_per_turn: int = Field(default=1, ge=1)
    per_target_concurrency: int = Field(default=1, ge=1)
    lease_ttl_seconds: float = Field(default=60.0, gt=0)
    drain_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Fraction of the action budget held back so a run always reaches
    #: verification/replanning with headroom. Only enforced when
    #: ``enforce_verification_reserve`` is on; otherwise the historical
    #: placeholder behaviour (the ratio is carried but inert) is preserved.
    verification_reserve_ratio: float = Field(default=0.2, ge=0.0, le=0.9)
    enforce_verification_reserve: bool = Field(default=False)
    branch_prune_failure_threshold: int = Field(default=2, ge=1)
    branch_prune_difficulty_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    branch_prune_objective_override: float = Field(default=0.8, ge=0.0, le=1.0)

    #: Legacy compatibility; the LLM-led runtime ignores ranking knobs.
    planner_score_margin: float = Field(default=0.15, ge=0.0, le=10.0)
    #: Retained for older settings; never limits LLM-generated actions.
    planner_rerank_top_k: int = Field(default=3, ge=2, le=20)

    #: One runtime mode; frozen legacy core flags do not select it.
    llm_candidate_generation: Literal[True] = True
    decision_max_attempts: int = Field(default=3, ge=1, le=6)
    # Normal plan/review decisions are not malformed-response retries.
    decision_normal_limit: int = Field(default=8, ge=1, le=32)
    # Explicit P2 selection. Target execution limits remain independent.
    specialist_decision_concurrency: int = Field(default=1, ge=1, le=2)
    specialist_decision_pool_limit: int | None = Field(default=None, ge=1)
    path_backend: Literal["memory", "sqlite"] = "memory"

    #: Overrides the value derived from ``max_steps`` when set.
    recursion_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _checkpoint_path_is_safe(self) -> OrchestrationSettings:
        if self.specialist_decision_concurrency > 1 and self.specialist_decision_pool_limit is None:
            raise ValueError("parallel_specialist_reasoning_requires_finite_pool")
        if self.checkpoint_backend == "sqlite" and not self.checkpoint_path.strip():
            raise ValueError("sqlite checkpointing needs a non-blank checkpoint_path")
        if self.work_backend == "sqlite" and not self.work_path.strip():
            raise ValueError("sqlite work backend needs a non-blank work_path")
        if self.task_graph_backend == "sqlite" and not self.task_graph_path.strip():
            raise ValueError("sqlite task graph backend needs a non-blank task_graph_path")
        if self.attack_graph_backend == "sqlite" and not self.attack_graph_path.strip():
            raise ValueError("sqlite attack graph backend needs a non-blank attack_graph_path")
        if self.operator_intent_backend == "sqlite" and not self.operator_intent_path.strip():
            raise ValueError(
                "sqlite operator intent backend needs a non-blank operator_intent_path"
            )
        return self

    @classmethod
    def for_durable_run(
        cls, config: OrchestrationConfig | None = None, *, max_steps: int | None = None
    ) -> OrchestrationSettings:
        """Use durable, independent domain stores for a CLI run and its resume."""
        return cls.from_config(
            config,
            max_steps=max_steps,
            checkpointing=True,
            checkpoint_backend="sqlite",
            work_backend="sqlite",
            task_graph_backend="sqlite",
            path_backend="sqlite",
            attack_graph_backend="sqlite",
            operator_intent_backend="sqlite",
            enforce_verification_reserve=True,
        )

    @property
    def effective_recursion_limit(self) -> int:
        if self.recursion_limit is not None:
            return self.recursion_limit
        return self.max_steps * NODES_PER_TURN + RECURSION_SLACK

    def verification_reserve(self, total_budget: int) -> int:
        """Actions held back from normal execution for verification.

        Clamped to ``[0, max(total_budget - 1, 0)]`` so at least one action
        can always run; a run with a single action reserves nothing. This is
        the scheduling primitive EXECUTE uses when
        ``enforce_verification_reserve`` is on.
        """
        if total_budget <= 0:
            return 0
        reserved = math.ceil(total_budget * self.verification_reserve_ratio)
        return min(reserved, max(total_budget - 1, 0))

    @classmethod
    def from_config(
        cls, config: OrchestrationConfig | None = None, **overrides: object
    ) -> OrchestrationSettings:
        """Read the shared ``AppConfig`` section, then apply overrides."""
        base: dict[str, object] = {}
        if config is not None:
            base["max_steps"] = config.max_steps
            base["max_retries"] = config.max_retries
            base["tool_timeout_seconds"] = config.tool_timeout_seconds
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(base)
