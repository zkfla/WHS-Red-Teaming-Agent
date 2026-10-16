"""Per-run orchestration limits.

Agent-Team-owned runtime shape; ``from_config`` bridges from the frozen
``core.config.OrchestrationConfig``. Execution policy only — nothing here
can widen what the Control Plane allows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from redteam.core.config import PLANNER_REPLAN_MAX_PASSES_CEILING, OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy

#: Prepare/checkpoint, model, tools, and settle each consume one graph step.
NODES_PER_TURN = 4

#: Head-room for the non-looping edges around the loop itself.
RECURSION_SLACK = 10

# Historical settings serialized into the first central binding. They never
# influenced execution. Keep their original defaults only in durable identity
# payloads so removing the selectable knobs does not invalidate saved runs.
_RETIRED_IDENTITY_DEFAULTS = {
    "branch_prune_failure_threshold": 2,
    "branch_prune_difficulty_threshold": 0.65,
    "branch_prune_objective_override": 0.8,
    "planner_score_margin": 0.15,
    "planner_rerank_top_k": 3,
}

_RETIRED_STATE_DEFAULTS = {
    "max_no_progress_turns": 3,
    "work_backend": "memory",
    "work_path": "data/orchestration/work.sqlite3",
    "task_graph_backend": "memory",
    "task_graph_path": "data/orchestration/task_graph.sqlite3",
    "attack_graph_backend": "memory",
    "attack_graph_path": "data/orchestration/attack_graph.sqlite3",
    "operator_intent_backend": "memory",
    "operator_intent_path": "data/orchestration/operator_intents.sqlite3",
    "path_backend": "memory",
    "max_actions_per_turn": 1,
    "verification_reserve_ratio": 0.2,
    "enforce_verification_reserve": False,
    # The retry knobs were advertised as execution policy, but the single
    # central runtime never retried a tool call (see ``execute_node``). They
    # are retained here purely so original bindings still decode.
    "max_retries": 3,
    "retry_retryable_tool_failures": True,
}

_LEGACY_ONLY_SETTINGS = frozenset(
    {
        "work_backend",
        "work_path",
        "task_graph_backend",
        "task_graph_path",
        "path_backend",
        "attack_graph_backend",
        "attack_graph_path",
        "operator_intent_backend",
        "operator_intent_path",
    }
)


class OrchestrationSettings(BaseModel):
    """Run limits shared by the planner, instance driver and native continuations."""

    _retired_identity: dict = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def retain_snapshot_identity(cls, value, handler):
        # Retired fields are passive input decoding for original SDK bindings.
        # They are absent from the public schema and never select a store/rule.
        if not isinstance(value, dict):
            return handler(value)
        originals = {key: value[key] for key in _RETIRED_STATE_DEFAULTS if key in value}
        result = handler({key: item for key, item in value.items() if key not in originals})
        result._retired_identity.update(originals)
        return result

    #: Hard ceiling on native model turns, independent of the
    #: Control Plane's ``ExecutionBudget``. Both must hold.
    max_steps: int = Field(default=8, ge=1)
    central_state_path: str = Field(default="data/orchestration/state.sqlite3", min_length=1)
    max_workers: int | None = Field(default=None, ge=1)
    # Production assembly requires an explicit request policy.
    llm_request_policy: LLMRequestPolicy | None = None
    specialist_profile_path: str | None = Field(default=None, min_length=1, max_length=4096)
    code_fixture_policy_path: str | None = Field(default=None, min_length=1, max_length=4096)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    #: Stop after this many tool executions fail back to back — a target
    #: that keeps erroring should not be hammered for the whole budget.
    max_consecutive_failures: int = Field(default=3, ge=1)
    #: Stop repeatable or cycling plans that produce no meaningful graph delta.

    observation_max_per_type: int = Field(default=20, ge=1)
    observation_max_total: int = Field(default=80, ge=1)

    #: LangGraph checkpointing: keeps a snapshot per super-step so a run
    #: can be inspected and replayed from an earlier branch.
    checkpointing: bool = True
    checkpoint_backend: Literal["memory", "sqlite"] = "memory"
    checkpoint_path: str = "data/orchestration/checkpoints.sqlite3"
    thread_id: str | None = None

    global_concurrency: int = Field(default=4, ge=1)
    #: Number of independent candidates admitted from one planning frontier.
    #: One preserves the original sequential graph; values above one enable
    #: the LangGraph ``Send`` map/reduce path.
    per_target_concurrency: int = Field(default=1, ge=1)
    lease_ttl_seconds: float = Field(default=60.0, gt=0)
    resource_wait_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    drain_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Fraction of the action budget held back so a run always reaches
    #: verification/replanning with headroom. Only enforced when
    #: ``enforce_verification_reserve`` is on; otherwise the historical
    #: placeholder behaviour (the ratio is carried but inert) is preserved.
    #: One runtime mode; frozen legacy core flags do not select it.
    llm_candidate_generation: Literal[True] = True
    decision_max_attempts: int = Field(default=12, ge=1, le=32)
    # Normal plan/review decisions are not malformed-response retries.
    decision_normal_limit: int = Field(default=24, ge=1, le=64)
    #: Separately counted transport-error retries inside the same chain
    #: total and deadline. 0 disables transport retries; never above 5.
    #: Scope note: ConversationAssistant keeps its own constructor limits
    #: (transport_limit=5 with its own timeout/max_attempts) and is NOT
    #: wired to this global setting, so changing this value never alters
    #: conversation behavior silently.
    decision_transport_max_attempts: int = Field(default=5, ge=0, le=5)
    #: Accepted internal planner follow-ups per Run; separate from the retired
    #: max_no_progress_turns identity field, which never limits execution.
    planner_replan_max_passes: int = Field(
        default=3, ge=0, le=PLANNER_REPLAN_MAX_PASSES_CEILING
    )
    decision_timeout_seconds: float = Field(
        default=900.0, gt=0, le=3600, allow_inf_nan=False
    )
    llm_attempt_timeout_seconds: float = Field(
        default=180.0, gt=0, le=900, allow_inf_nan=False
    )

    #: Overrides the value derived from ``max_steps`` when set.
    recursion_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _checkpoint_path_is_safe(self) -> OrchestrationSettings:
        if self.checkpoint_backend == "sqlite" and not self.checkpoint_path.strip():
            raise ValueError("sqlite checkpointing needs a non-blank checkpoint_path")
        return self

    @classmethod
    def for_durable_run(
        cls, config: OrchestrationConfig | None = None, *, max_steps: int | None = None
    ) -> OrchestrationSettings:
        """Persist central execution checkpoints; domain state has one owner."""
        return cls.from_config(
            config,
            max_steps=max_steps,
            checkpointing=True,
            checkpoint_backend="sqlite",
            enforce_verification_reserve=True,
        )

    @property
    def effective_recursion_limit(self) -> int:
        if self.recursion_limit is not None:
            return self.recursion_limit
        return self.max_steps * NODES_PER_TURN + RECURSION_SLACK

    def durable_payload(self) -> dict:
        """Preserve the original settings identity, including retired defaults."""
        return {
            **_RETIRED_IDENTITY_DEFAULTS,
            **_RETIRED_STATE_DEFAULTS,
            **self._retired_identity,
            **self.model_dump(mode="json"),
        }

    def central_payload(self) -> dict:
        """Operative settings and historical identity fields, without legacy DB path copies."""
        historical = {key: self._retired_identity.get(key, value)
                      for key, value in _RETIRED_STATE_DEFAULTS.items()
                      if key not in _LEGACY_ONLY_SETTINGS}
        return {**historical, **self.model_dump(mode="json")}

    def with_overrides(self, **overrides) -> OrchestrationSettings:
        """Validate selected changes without discarding original passive identity."""
        return type(self).model_validate({**self.durable_payload(), **overrides})

    @classmethod
    def from_config(
        cls, config: OrchestrationConfig | None = None, **overrides: object
    ) -> OrchestrationSettings:
        """Read the shared ``AppConfig`` section, then apply overrides."""
        base: dict[str, object] = {}
        if config is not None:
            base["max_steps"] = config.max_steps
            base["central_state_path"] = config.central_state_path
            base["max_workers"] = config.max_workers
            base["tool_timeout_seconds"] = config.tool_timeout_seconds
            base["resource_wait_timeout_seconds"] = config.resource_wait_timeout_seconds
            base["llm_request_policy"] = config.llm_request_policy
            base["specialist_profile_path"] = config.specialist_profile_path
            base["code_fixture_policy_path"] = config.code_fixture_policy_path
            base["decision_normal_limit"] = config.decision_normal_limit
            base["decision_max_attempts"] = config.decision_max_attempts
            base["decision_transport_max_attempts"] = config.decision_transport_max_attempts
            base["planner_replan_max_passes"] = config.planner_replan_max_passes
            base["decision_timeout_seconds"] = config.decision_timeout_seconds
            base["llm_attempt_timeout_seconds"] = config.llm_attempt_timeout_seconds
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(base)
