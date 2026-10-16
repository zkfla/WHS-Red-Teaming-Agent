"""Mission, Task Graph and operator-intent domain contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.specialists import ProbeLimits
from redteam.core.enums import MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import (
    AgentId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    HypothesisId,
    MissionId,
    OperatorIntentId,
    TaskId,
)
from redteam.core.redaction import OPAQUE_SUFFIXES, SECRET_KEY_PARTS, redact_sensitive_text


def _is_secret_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    if normalized.endswith(OPAQUE_SUFFIXES):
        return False
    return any(part in normalized for part in SECRET_KEY_PARTS)


def _reject_secret_material(value: Any, *, path: str = "metadata") -> Any:
    """Reject secret-shaped fields while allowing opaque ``*_ref`` values.

    Raises a ``ValidationError`` built with ``hide_input=True``: Pydantic's
    default error rendering would otherwise echo the offending mapping —
    including the raw secret — into the exception text/logs.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key):
                raise _secret_field_error(path, key)
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")
    return value


def _secret_field_error(path: str, key: Any) -> ValidationError:
    """A ``ValidationError`` naming *where* the secret was, never its value."""
    return ValidationError.from_exception_data(
        "SecretMaterialNotPermitted",
        [
            InitErrorDetails(
                type=PydanticCustomError(
                    "raw_secret_field",
                    "raw secret field is not permitted at {path}",
                    {"path": f"{path}.{key}"},
                ),
                loc=(str(key),),
                input=None,
            )
        ],
        hide_input=True,
    )


def redact_secret_material(value: Any, *, drop_secret_keys: bool = True) -> Any:
    """Recursively strip raw secret material from a JSON-shaped payload.

    This is the *boundary* counterpart of :func:`_reject_secret_material`:
    secret-shaped **keys** are dropped (an opaque ``*_ref``/``*_id`` key is
    preserved) and **free-text values** are passed through
    :func:`redteam.core.redaction.redact_sensitive_text`. It exists because
    field validators only run at construction, while ``model_copy(update=...)``
    — used throughout the Orchestrator — bypasses them. Stores run it before
    persisting and when loading legacy payloads, so a raw secret can never be
    written or replayed. Never mutates its input.
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if drop_secret_keys and _is_secret_key(key):
                continue
            redacted[str(key)] = redact_secret_material(item, drop_secret_keys=drop_secret_keys)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_secret_material(item, drop_secret_keys=drop_secret_keys) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


class Provenance(BaseModel):
    source: str = Field(min_length=1)
    references: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        return redact_sensitive_text(value.strip())

    @field_validator("references")
    @classmethod
    def _stable_references(cls, value: list[str]) -> list[str]:
        return sorted({redact_sensitive_text(item.strip()) for item in value if item.strip()})

    @field_validator("metadata")
    @classmethod
    def _metadata_has_no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_secret_material(value, path="provenance.metadata")
        return redact_secret_material(value)


class MissionCriterionResult(BaseModel):
    criterion: str
    met: bool
    task_ids: list[TaskId] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    result_claims: list[dict[str, Any]] = Field(default_factory=list)
    #: The result meaning judged by the model; None on legacy records.
    purpose_hash: str | None = None
    goal_revision: int | None = Field(default=None, ge=1)

    @field_validator("criterion")
    @classmethod
    def _criterion_redacted(cls, value: str) -> str:
        return redact_sensitive_text(value.strip())

    @field_validator("reason")
    @classmethod
    def _reason_redacted(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value) if value is not None else None


class MissionPlan(BaseModel):
    mission_id: MissionId
    engagement_id: EngagementId
    objective: str = Field(min_length=1)
    priorities: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    criterion_results: list[MissionCriterionResult] = Field(default_factory=list)
    initial_workstreams: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    operator_constraints: dict[str, Any] = Field(default_factory=dict)
    operator_preferences: dict[str, Any] = Field(default_factory=dict)
    status: MissionStatus = MissionStatus.DRAFT
    revision: int = Field(default=1, ge=1)
    termination_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    provenance: Provenance

    @field_validator("objective", "termination_reason")
    @classmethod
    def _objective_not_blank(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value.strip()) if value is not None else None

    @field_validator("priorities", "success_criteria", "initial_workstreams", "assumptions")
    @classmethod
    def _stable_text_lists(cls, value: list[str]) -> list[str]:
        return list(
            dict.fromkeys(redact_sensitive_text(item.strip()) for item in value if item.strip())
        )

    @field_validator("operator_constraints", "operator_preferences")
    @classmethod
    def _operator_data_has_no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_secret_material(value, path="operator_data")
        return redact_secret_material(value)

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> MissionPlan:
        if self.updated_at < self.created_at:
            # updated_at can never logically precede created_at. A stale or
            # clock-skewed operation `now` stamped on an update is clamped to
            # created_at rather than crashing the pipeline (clocks are not
            # guaranteed monotonic, and an operation timestamp may be reused).
            self.updated_at = self.created_at
        return self

    def redacted_copy(self) -> MissionPlan:
        """Return a re-validated, secret-free copy for a persistence boundary.

        ``model_copy(update=...)`` skips field validators, so a caller can
        inject a raw secret into an otherwise-valid Mission. This dumps,
        redacts, then re-validates so no raw secret can be persisted. Raises
        ``ValidationError`` only for genuinely invalid data — after redaction,
        so an error message can never echo the raw secret.
        """
        payload = redact_secret_material(self.model_dump(mode="python"))
        return MissionPlan.model_validate(payload)


class ResultQuestion(BaseModel):
    """An item of a task result, without an independent question lifecycle."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    required_for_goal: bool = False
    reason: str | None = None

    @field_validator("question", "reason")
    @classmethod
    def _safe_text(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value.strip()) if value is not None else None

    @model_validator(mode="after")
    def _required_reason(self) -> ResultQuestion:
        if not self.question_id.strip():
            raise PydanticCustomError(
                "question_id_blank", "result question id must be nonblank"
            )
        if not self.question.strip():
            raise PydanticCustomError(
                "question_text_blank", "result question must be nonblank"
            )
        if self.required_for_goal and not self.reason:
            raise PydanticCustomError(
                "question_required_reason_missing",
                "goal-required question needs a reason",
            )
        return self


class InvestigationResult(BaseModel):
    """Investigation outcome; does not assert a Finding or Mission completion."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    result_id: str = Field(min_length=1)
    outcome: Literal["confirmed", "refuted", "inconclusive", "blocked"]
    summary: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    hypothesis_ids: list[HypothesisId] = Field(default_factory=list)
    finding_node_ids: list[GraphNodeId] = Field(default_factory=list)
    completion_receipt_id: EvidenceId | None = None
    questions: list[ResultQuestion] = Field(default_factory=list)
    # v2 outcome concerns the probe purpose, never the related candidate's verdict.
    contract_version: Literal[1, 2] = 1
    observations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    assessment_drafts: list[AssessmentDraft] = Field(default_factory=list, max_length=32)

    @field_validator("observations", "limitations")
    @classmethod
    def _safe_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise PydanticCustomError(
                "result_observation_blank",
                "result observations and limitations must be nonblank",
            )
        return list(dict.fromkeys(redact_sensitive_text(value.strip()) for value in values))

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if not self.assessment_drafts:
            result.pop("assessment_drafts", None)
        if self.contract_version == 1:
            for key in ("contract_version", "observations", "limitations"):
                result.pop(key, None)
        return result

    @field_validator("summary")
    @classmethod
    def _safe_summary(cls, value: str) -> str:
        return redact_sensitive_text(value.strip())

    @model_validator(mode="after")
    def _unique_questions(self) -> InvestigationResult:
        ids = [item.question_id for item in self.questions]
        if not self.result_id.strip():
            raise PydanticCustomError("result_id_blank", "result id must be nonblank")
        if not self.summary:
            raise PydanticCustomError("result_summary_blank", "result summary must be nonblank")
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "result_question_duplicate", "result question ids must be unique"
            )
        draft_ids = [draft.draft_id for draft in self.assessment_drafts]
        if len(draft_ids) != len(set(draft_ids)):
            raise PydanticCustomError(
                "result_assessment_duplicate", "result assessment draft ids must be unique",
            )
        if self.contract_version == 1 and (
            self.observations or self.limitations or self.assessment_drafts
        ):
            raise PydanticCustomError(
                "result_v1_extensions_forbidden",
                "structured probe observations require result contract v2",
            )
        return self

    def assessment_evidence_ids(self) -> list[str]:
        return list(dict.fromkeys(
            ref for draft in self.assessment_drafts for ref in draft.all_evidence_ids()
        ))


class MissionTask(BaseModel):
    task_id: TaskId
    mission_id: MissionId
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    completion_contract_version: Literal[1, 2] = 1
    investigation_contract_version: Literal[1, 2] = 1
    probe_limits: ProbeLimits | None = None
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list)
    principal_refs: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    required: bool = True
    assigned_agent: AgentId | None = None
    #: Instance lease owner holding this task (``work_owner`` string,
    #: e.g. ``"<run_id>:instance:<instance_id>"`` or legacy ``"<run_id>"``).
    #: Distinct from ``assigned_agent`` (role). ``None`` means unclaimed:
    #: claimable by the first claimant (legacy-row migration path).
    #: No TTL — an unfinished claim is released explicitly, never by expiry.
    occupant_owner: str | None = None
    # Central storage uses this generation, not an expiring TaskClaim.
    ownership_version: int = Field(default=0, ge=0)
    scope_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    result: InvestigationResult | None = None
    source_task_id: TaskId | None = None
    source_result_id: str | None = None
    source_question_id: str | None = None
    required_capability: str | None = None
    parent_task_id: TaskId | None = None
    dependency_task_ids: list[TaskId] = Field(default_factory=list)
    blocking_reason: str | None = None
    # Committed atomically with completion; a review alone cannot complete a task.
    completion_evidence_id: EvidenceId | None = None
    related_attack_graph_node_ids: list[GraphNodeId] = Field(default_factory=list)
    related_hypothesis_ids: list[HypothesisId] = Field(default_factory=list)
    # Context relevance is independent of the goal's required hypothesis proofs.
    goal_required_hypothesis_ids: list[HypothesisId] = Field(default_factory=list)
    hypothesis_revisions: dict[str, int] = Field(default_factory=dict)
    provenance: Provenance
    created_at: datetime
    updated_at: datetime

    @field_validator("title", "objective")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return redact_sensitive_text(value.strip())

    @field_validator("blocking_reason")
    @classmethod
    def _blocking_reason_redacted(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value) if value is not None else None

    @field_validator(
        "dependency_task_ids", "related_attack_graph_node_ids", "related_hypothesis_ids",
        "principal_refs",
    )
    @classmethod
    def _stable_ids(cls, value: list[Any]) -> list[Any]:
        return sorted(set(value), key=str)

    @model_validator(mode="after")
    def _task_invariants(self) -> MissionTask:
        if self.probe_limits is not None and self.investigation_contract_version != 2:
            raise ValueError("bounded probes require investigation contract v2")
        source = (self.source_task_id, self.source_result_id, self.source_question_id)
        if self.investigation_contract_version == 1 and any(source) and not all(source):
            raise ValueError("follow-up task needs its source task, result and question")
        if self.investigation_contract_version == 2:
            if bool(self.source_task_id) != bool(self.source_result_id) or (
                self.source_question_id and not self.source_task_id
            ):
                raise ValueError("probe follow-up needs its source task and result")
            if self.completion_contract_version != 2:
                raise ValueError("probe tasks require explicit purpose conditions")
            related = set(map(str, self.related_hypothesis_ids))
            if set(self.hypothesis_revisions) != related or any(
                type(value) is not int or value < 1 for value in self.hypothesis_revisions.values()
            ):
                raise ValueError("probe tasks need each related candidate's admitted revision")
            if set(map(str, self.goal_required_hypothesis_ids)) - related:
                raise ValueError("goal-required candidates must be related to the probe")
            if self.goal_required_hypothesis_ids and not self.required:
                raise ValueError("goal-required candidates need a goal-required task")
            if self.result is not None and self.result.contract_version != 2:
                raise ValueError("probe tasks require result contract v2")
        if self.completion_contract_version == 1 and self.completion_criteria:
            raise ValueError("explicit completion criteria require task contract v2")
        if self.completion_contract_version == 2:
            if not self.completion_criteria or any(
                not item.is_known() or not item.description.strip()
                for item in self.completion_criteria
            ):
                raise ValueError("task contract v2 requires nonempty known completion criteria")
            identities = [criterion_id(item) for item in self.completion_criteria]
            if len(identities) != len(set(identities)):
                raise ValueError("task completion criteria must not be duplicated")
            for condition in self.completion_criteria:
                if set(condition.principal_refs) - set(self.principal_refs):
                    raise ValueError("task completion principal outside task subjects")
                if condition.hypothesis_id and condition.hypothesis_id not in set(
                    map(str, self.related_hypothesis_ids)
                ):
                    raise ValueError("task completion hypothesis outside task subjects")
        if self.task_id in self.dependency_task_ids:
            raise ValueError("task cannot depend on itself")
        if self.status is TaskStatus.BLOCKED and not (self.blocking_reason or "").strip():
            raise ValueError("blocked task requires blocking_reason")
        if self.status is not TaskStatus.BLOCKED and self.blocking_reason is not None:
            raise ValueError("blocking_reason is only valid for blocked tasks")
        if self.updated_at < self.created_at:
            # See MissionPlan._timestamps_are_ordered: clamp, don't crash.
            self.updated_at = self.created_at
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if self.probe_limits is None:
            result.pop("probe_limits", None)
        if self.investigation_contract_version == 1:
            for key in ("investigation_contract_version", "goal_required_hypothesis_ids",
                        "hypothesis_revisions"):
                result.pop(key, None)
        return result

    def goal_hypothesis_ids(self) -> list[HypothesisId]:
        """Keep v1 completion meaning; v2 links alone impose no final verdict."""
        return (self.related_hypothesis_ids if self.investigation_contract_version == 1
                else self.goal_required_hypothesis_ids)

    def redacted_copy(self) -> MissionTask:
        """Return a re-validated, secret-free copy for a persistence boundary.

        Closes the ``model_copy(update=...)`` bypass used by
        ``transition_task`` (whose ``blocking_reason`` is free text) and by
        callers that mutate a Task before ``update_task``. Redaction runs
        before validation, so a raised ``ValidationError`` never echoes a raw
        secret.
        """
        payload = redact_secret_material(self.model_dump(mode="python"))
        return MissionTask.model_validate(payload)


class OperatorIntent(BaseModel):
    intent_id: OperatorIntentId
    mission_id: MissionId
    intent_type: OperatorIntentType
    instruction: str = Field(min_length=1)
    task_ids: list[TaskId] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    created_at: datetime

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, value: str) -> str:
        return redact_sensitive_text(value.strip())

    @field_validator("task_ids")
    @classmethod
    def _stable_task_ids(cls, value: list[TaskId]) -> list[TaskId]:
        return sorted(set(value), key=str)

    @field_validator("parameters")
    @classmethod
    def _parameters_are_non_executable_and_secret_free(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        forbidden = {"tool_id", "tool_invocation", "mcp_invocation", "command", "payload"}
        overlap = sorted({str(key).lower() for key in value} & forbidden)
        if overlap:
            raise ValueError(f"operator intent cannot contain executable fields: {overlap}")
        _reject_secret_material(value, path="intent.parameters")
        return redact_secret_material(value)

    def redacted_copy(self) -> OperatorIntent:
        """Return a re-validated, secret-free copy for a persistence boundary."""
        payload = redact_secret_material(self.model_dump(mode="python"))
        return OperatorIntent.model_validate(payload)
