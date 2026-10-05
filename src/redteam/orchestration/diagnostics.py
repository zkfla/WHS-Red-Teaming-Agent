"""Safe, durable failure descriptions owned by orchestration.

Validation rules and known field locations are sufficient for correction.
Never copy exception messages/inputs, full model responses or private reasoning.
"""

from __future__ import annotations

import json
import math
from itertools import islice
from typing import Any, Literal

from jsonschema import ValidationError as SchemaValidationError
from pydantic import BaseModel, Field, ValidationError, field_validator

from redteam.core.action_semantics import ActionNamingError
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.redaction import redact_sensitive_text
from redteam.orchestration.evidence_access import EvidenceReadRequest

_READ_SCHEMA = EvidenceReadRequest.model_json_schema()
_READ_FIELDS = frozenset(_READ_SCHEMA["properties"])

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
) | _READ_FIELDS


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
    validation_issues: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    termination_reason: Literal["budget_exhausted"] | None = None

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
        rule = f" [{self.validation_issues[0]['rule']}]" if self.validation_issues else ""
        return safe_feedback(f"{self.code}{location}{rule}: {self.summary}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "non_json"


def _value_shape(value: Any, *, control_number: bool = False) -> dict[str, Any]:
    """Shallow JSON structure; strings and container contents are never copied."""
    kind = _json_type(value)
    shape: dict[str, Any] = {"type": kind}
    if kind == "integer":
        if control_number:
            if value.bit_length() <= 1024:
                shape["value"] = value
            else:
                shape["bits"] = value.bit_length()
    elif kind == "number":
        if control_number:
            if math.isfinite(value):
                shape["value"] = value
            else:
                shape["finite"] = False
    elif kind == "string":
        shape["length"] = len(value)
    elif kind == "array":
        shape.update(
            length=len(value), item_types=sorted({_json_type(item) for item in value[:10]}),
        )
    elif kind == "object":
        shape["field_count"] = len(value)
    return shape


def evidence_request_shape(value: Any) -> dict[str, Any]:
    """Use the actual public read contract; unknown keys remain unnamed."""
    shape = _value_shape(value)
    if not isinstance(value, dict):
        return shape
    shape["fields"] = {
        name: _value_shape(
            value[name],
            control_number=spec.get("type") in {"integer", "number"},
        )
        for name, spec in _READ_SCHEMA["properties"].items()
        if name in value
    }
    unknown_values = (item for key, item in value.items() if key not in _READ_FIELDS)
    shape["unknown_field_count"] = len(value) - len(shape["fields"])
    shape["unknown_field_shapes"] = [_value_shape(item) for item in islice(unknown_values, 10)]
    return shape


# These are fixed CPython decoder messages, not model/provider response text.
# Unknown or dynamically formatted messages never enter diagnostics verbatim.
_JSON_SYNTAX_ERRORS = {
    "Expecting value": ("expected_value", "Supply a JSON value at the reported position."),
    "Expecting property name enclosed in double quotes": (
        "expected_property_name", "Use double-quoted property names and no trailing comma.",
    ),
    "Expecting ':' delimiter": ("expected_colon", "Put a colon between each property and value."),
    "Expecting ',' delimiter": (
        "expected_comma", "Separate adjacent values with commas and close all containers.",
    ),
    "Unterminated string starting at": (
        "unterminated_string", "Close every JSON string and escape embedded quotes.",
    ),
    "Extra data": ("extra_data", "Return one JSON object without trailing content."),
    "Invalid \\uXXXX escape": (
        "invalid_unicode_escape", "Use four hexadecimal digits in each Unicode escape.",
    ),
    "Illegal trailing comma before end of object": (
        "trailing_comma", "Remove the comma before the closing delimiter.",
    ),
    "Illegal trailing comma before end of array": (
        "trailing_comma", "Remove the comma before the closing delimiter.",
    ),
}

# CPython's two scanner variants differ here: the C scanner emits a bare
# "Invalid \\escape" while the pure-Python decoder appends the character.
# Only the fixed code-owned prefix is matched; the tail is never copied.
_JSON_SYNTAX_PREFIXES = (
    ("Invalid control character", (
        "invalid_control_character", "Escape control characters inside JSON strings.",
    )),
    ("Invalid \\escape", ("invalid_escape", "Use valid JSON escapes inside strings.")),
)


def _json_syntax_hint(msg: str) -> tuple[str, str]:
    """Map a decoder message to a public reason and a fixed correction."""
    exact = _JSON_SYNTAX_ERRORS.get(msg)
    if exact is not None:
        return exact
    for prefix, mapped in _JSON_SYNTAX_PREFIXES:
        if msg.startswith(prefix):
            return mapped
    return "unknown", ""


def json_parse_context(exc: json.JSONDecodeError) -> dict[str, Any]:
    """Coordinates describe the decoder input, never the original response."""
    first = exc.doc.lstrip()[:1]
    return {
        "proposal_shape": {"observed": False, "reason": "unparsed"},
        "parse_error": {
            "coordinate_source": "decoder_input",
            "line": exc.lineno,
            "column": exc.colno,
            "position": exc.pos,
            "input_length": len(exc.doc),
            "syntax_reason": _json_syntax_hint(exc.msg)[0],
            "at_input_end": exc.pos >= len(exc.doc),
            "start_category": "object" if first == "{" else "array" if first == "["
            else "other" if first else "empty",
        },
    }


def decision_context(
    payload: dict, proposal: Any = None, *, response_schema: dict | None = None,
    validated: bool = True,
) -> dict:
    """Project execution-relevant references, never raw prompts or arguments."""
    path = payload.get("draft_path") or payload.get("active_path") or {}
    steps = path.get("steps", []) if isinstance(path, dict) else []
    if not isinstance(steps, list):
        steps = []
    if isinstance(payload.get("step"), dict):
        steps = [payload["step"]]
    feedback = payload.get("feedback", [])
    context: dict[str, Any] = {
        "steps": [
            {
                key: step[key]
                for key in ("step_id", "status", "depends_on", "required_fact_refs")
                if key in step
            }
            for step in steps[:10]
            if isinstance(step, dict)
        ],
        "feedback": [safe_feedback(item) for item in feedback[:10] if isinstance(item, str)]
        if isinstance(feedback, list) else [],
    }
    if not isinstance(feedback, list):
        context["feedback_shape"] = _value_shape(feedback)
    if not validated:
        context["proposal_shape"] = _value_shape(proposal)
        if isinstance(proposal, dict):
            context["proposal"] = {
                f"{key}_shape": _value_shape(proposal[key])
                for key in ("mode", "decision", "path_step_id", "accepted", "reason", "actions")
                if key in proposal
            }
            if "evidence_request" in proposal:
                context["proposal"]["evidence_request_shape"] = evidence_request_shape(
                    proposal["evidence_request"]
                )
        return redact_secret_material(context)
    if proposal is not None and not isinstance(proposal, dict):
        context["proposal_shape"] = _value_shape(proposal)
    elif isinstance(proposal, dict):
        proposal_view: dict[str, Any] = {
            key: proposal[key]
            for key in ("mode", "decision", "path_step_id", "accepted", "reason")
            if key in proposal
        }
        if response_schema is not None:
            for name in ("mode", "decision"):
                allowed = response_schema.get("properties", {}).get(name, {}).get("enum")
                if name in proposal_view and allowed and proposal_view[name] not in allowed:
                    proposal_view[f"{name}_shape"] = _value_shape(proposal_view.pop(name))
        context["proposal"] = proposal_view
        if "reason" in proposal_view:
            proposal_view["reason"] = safe_feedback(str(proposal_view["reason"]))
        if "evidence_request" in proposal:
            proposal_view["evidence_request_shape"] = evidence_request_shape(
                proposal["evidence_request"]
            )
        actions = proposal.get("actions", [])
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
            for action in actions[:10]
            if isinstance(action, dict)
        ] if isinstance(actions, list) else []
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
    fields = _schema_fields(schema)
    locations = [known_location(exc.absolute_path, fields=fields)]
    if rule == "required" and isinstance(exc.instance, dict):
        locations = [
            known_location([*exc.absolute_path, field], fields=fields)
            for field in exc.validator_value
            if field in fields and field not in exc.instance
        ][:10] or locations
    return DecisionDiagnostic(
        code=f"schema_{rule}",
        stage=_identifier(stage, "decision"),
        kind="format",
        location=locations[0],
        summary=rules.get(rule, "Match the required JSON schema."),
        retryable=True,
        validation_issues=[{"rule": rule, "location": location} for location in locations],
    )


def diagnostic_for_exception(exc: BaseException, *, stage: str) -> DecisionDiagnostic:
    attached = getattr(exc, "diagnostic", None)
    if isinstance(attached, DecisionDiagnostic):
        return attached
    if isinstance(exc, json.JSONDecodeError):
        location = f"decoder input line {exc.lineno}, column {exc.colno}"
        _, correction = _json_syntax_hint(exc.msg)
        return DecisionDiagnostic(
            code="invalid_json", stage=_identifier(stage, "decision"), kind="format",
            location=location, summary="Return a valid JSON object. " + correction,
            retryable=True,
            validation_issues=[{"rule": "json_decode", "location": location}],
        )
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
        issues = [
            {"rule": _identifier(str(error.get("type", "")), "validation"),
             "location": known_location(error.get("loc", ()))}
            for error in errors[:10]
        ]
        action_rules = {
            "unknown_operation": "Use a supported action capability and operation.",
            "tool_outside_capability_namespace": (
                "Choose a tool from the action's capability namespace."
            ),
        }
        rule = str(first.get("type") or "")
        if rule in action_rules:
            return DecisionDiagnostic(
                code=rule,
                stage=_identifier(stage, "decision"),
                kind="format",
                location=known_location(first.get("loc", ())),
                summary=action_rules[rule],
                retryable=True,
                validation_issues=issues,
            )
        summaries = {
            "missing": "Include the required field.",
            "string_type": "Use a string value.",
            "string_too_short": "Provide a nonempty string of the required length.",
            "int_type": "Use an integer value.",
            "int_parsing": "Use a valid integer value.",
            "int_from_float": "Use an integer without a fractional part.",
            "greater_than_equal": "Meet the minimum in the public request schema.",
            "less_than_equal": "Respect the maximum in the public request schema.",
            "extra_forbidden": "Remove unsupported fields.",
        }
        return DecisionDiagnostic(
            code="model_validation",
            stage=_identifier(stage, "decision"),
            kind="format",
            location=known_location(first.get("loc", ())),
            summary=summaries.get(rule, "Match the required model fields and validation rules."),
            retryable=True,
            validation_issues=issues,
        )
    return DecisionDiagnostic(
        code="invalid_decision",
        stage=_identifier(stage, "decision"),
        kind="format",
        summary="The decision did not satisfy an orchestration validation rule.",
        retryable=True,
    )
