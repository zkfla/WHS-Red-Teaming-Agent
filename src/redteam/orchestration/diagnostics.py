"""Safe, durable failure descriptions owned by orchestration.

Validation rules and known field locations are sufficient for correction.
Never copy a validation exception's message, input, or a model response.
"""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError as SchemaValidationError
from pydantic import BaseModel, Field, ValidationError, field_validator

from redteam.core.action_semantics import ActionNamingError
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.redaction import redact_sensitive_text

_KNOWN_FIELDS = frozenset(
    {
        "mode",
        "path",
        "steps",
        "completion_criteria",
        "criteria",
        "kind",
        "description",
        "min_evidence",
        "hypothesis_id",
        "principal_refs",
        "actions",
        "path_step_id",
        "agent_id",
        "target_node_id",
        "target_node_ids",
        "capability",
        "operation",
        "tool_id",
        "arguments",
        "reason",
        "goal",
        "task_id",
        "mission_id",
        "run_id",
        "path_id",
        "version",
        "parent_version",
        "status",
        "step_id",
        "depends_on",
        "required",
        "evidence_ids",
        "completion",
        "review",
        "accepted",
        "feedback",
        "revisions",
        "hypotheses",
        "title",
        "tasks",
        "success_criteria",
    }
)


def safe_feedback(text: str, limit: int = 500) -> str:
    """Redact first, then bound public feedback and durable summaries."""
    return " ".join(redact_sensitive_text(text).split())[:limit]


def _identifier(value: str, fallback: str) -> str:
    if not value or len(value) > 100:
        return fallback
    return value if all(char.isalnum() or char in "_-" for char in value) else fallback


class DecisionDiagnostic(BaseModel):
    code: str
    stage: str
    kind: str
    attempts: int = Field(default=0, ge=0)
    location: str = Field(default="", max_length=200)
    summary: str = Field(max_length=500)
    retryable: bool = False
    last_cause: DecisionDiagnostic | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    attempt_limit: int | None = None
    stage_attempts: dict[str, int] = Field(default_factory=dict)
    normal_limit: int | None = None
    repair_limit: int | None = None
    normal_used: int = Field(default=0, ge=0)
    repair_used: int = Field(default=0, ge=0)
    provider_status: int | None = None
    provider_code: str | None = None
    retry_after: float | None = None

    @field_validator("trace", mode="before")
    @classmethod
    def _safe_trace(cls, value):
        return redact_secret_material(value[-20:])

    @field_validator("summary", mode="before")
    @classmethod
    def _safe_summary(cls, value: str) -> str:
        return safe_feedback(value)

    @field_validator("location", mode="before")
    @classmethod
    def _bounded_location(cls, value: str) -> str:
        return safe_feedback(value, 200)

    def feedback(self) -> str:
        location = f" at {self.location}" if self.location else ""
        return safe_feedback(f"{self.code}{location}: {self.summary}")


def decision_context(payload: dict, proposal: dict | None = None) -> dict:
    """Project execution-relevant references, never raw prompts or arguments."""
    path = payload.get("draft_path") or payload.get("active_path") or {}
    steps = path.get("steps", [])
    if payload.get("step"):
        steps = [payload["step"]]
    context = {
        "steps": [
            {
                key: step[key]
                for key in ("step_id", "status", "depends_on", "required_fact_refs")
                if key in step
            }
            for step in steps[:10]
        ],
        "feedback": [safe_feedback(str(item)) for item in payload.get("feedback", [])[:10]],
    }
    if proposal is not None:
        proposal_view: dict[str, Any] = {
            key: proposal[key]
            for key in ("mode", "decision", "path_step_id", "accepted", "reason")
            if key in proposal
        }
        context["proposal"] = proposal_view
        if "reason" in proposal_view:
            proposal_view["reason"] = safe_feedback(str(proposal_view["reason"]))
        context["actions"] = [
            {
                key: action[key]
                for key in (
                    "path_step_id",
                    "agent_id",
                    "target_node_id",
                    "capability",
                    "operation",
                    "tool_id",
                )
                if key in action
            }
            for action in proposal.get("actions", [])[:10]
        ]
    return redact_secret_material(context)


def known_location(parts: Any, *, fields: frozenset[str] = _KNOWN_FIELDS) -> str:
    """Discard unknown mapping keys; they can be arbitrary model input."""
    location = "$"
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool) and part >= 0:
            location += f"[{part}]"
        elif isinstance(part, str) and part in fields:
            location += f".{part}"
        else:
            break
    return location[:200]


def _schema_fields(schema: Any) -> frozenset[str]:
    fields: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            fields.update(key for key in properties if isinstance(key, str))
        for value in schema.values():
            fields.update(_schema_fields(value))
    elif isinstance(schema, list):
        for value in schema:
            fields.update(_schema_fields(value))
    return frozenset(fields)


def schema_diagnostic(
    exc: SchemaValidationError, schema: dict[str, Any], *, stage: str
) -> DecisionDiagnostic:
    # Validator names are library-owned; messages/validator values can contain input.
    rules = {
        "type": "Use the required JSON type.",
        "required": "Include all required fields.",
        "enum": "Use an allowed value.",
        "additionalProperties": "Remove unsupported fields.",
        "minLength": "Provide a nonempty string of the required length.",
        "minimum": "Meet the required minimum number.",
        "minItems": "Include the required number of items.",
        "maximum": "Respect the maximum number.",
        "maxItems": "Respect the maximum number of items.",
        "maxLength": "Respect the maximum string length.",
        "anyOf": "Match one permitted schema alternative.",
        "oneOf": "Match exactly one permitted schema alternative.",
        "const": "Use the required constant value.",
        "pattern": "Use the required string format.",
    }
    rule = exc.validator if exc.validator in rules else "validation"
    return DecisionDiagnostic(
        code=f"schema_{rule}",
        stage=_identifier(stage, "decision"),
        kind="format",
        location=known_location(exc.absolute_path, fields=_schema_fields(schema)),
        summary=rules.get(rule, "Match the required JSON schema."),
        retryable=True,
    )


def diagnostic_for_exception(exc: BaseException, *, stage: str) -> DecisionDiagnostic:
    attached = getattr(exc, "diagnostic", None)
    if isinstance(attached, DecisionDiagnostic):
        return attached
    if isinstance(exc, ActionNamingError):
        summaries = {
            "capability_unsupported": "Use a supported capability namespace.",
            "operation_invalid_format": "Use a lower_snake_case action name.",
            "operation_unsupported": "Use a supported action meaning.",
        }
        return DecisionDiagnostic(
            code=exc.reason,
            stage=_identifier(stage, "decision"),
            kind="format",
            location=known_location((exc.field,)),
            summary=summaries.get(exc.reason, "Use a supported action name."),
            retryable=True,
        )
    if isinstance(exc, ValidationError):
        # include_input=False is mandatory even though messages are not copied.
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        first: dict[str, Any] = dict(errors[0]) if errors else {}
        action_rules = {
            "unknown_operation": "Use a supported action capability and operation.",
            "tool_outside_capability_namespace": (
                "Choose a tool from the action's capability namespace."
            ),
        }
        rule = first.get("type")
        if rule in action_rules:
            return DecisionDiagnostic(
                code=rule,
                stage=_identifier(stage, "decision"),
                kind="format",
                location=known_location(first.get("loc", ())),
                summary=action_rules[rule],
                retryable=True,
            )
        return DecisionDiagnostic(
            code="model_validation",
            stage=_identifier(stage, "decision"),
            kind="format",
            location=known_location(first.get("loc", ())),
            summary="Match the required model fields and validation rules.",
            retryable=True,
        )
    return DecisionDiagnostic(
        code="invalid_decision",
        stage=_identifier(stage, "decision"),
        kind="format",
        summary="The decision did not satisfy an orchestration validation rule.",
        retryable=True,
    )
