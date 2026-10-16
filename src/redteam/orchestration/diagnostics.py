"""Safe, durable failure descriptions owned by orchestration.

Validation rules and known field locations are sufficient for correction.
Never copy exception messages/inputs, full model responses or private reasoning.
"""

from __future__ import annotations

import json
import math
from itertools import islice
from types import TracebackType
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
        "claims",
        "accepted",
        "feedback",
        "revisions",
        "hypotheses",
        "title",
        "tasks",
        "success_criteria",
    }
) | _READ_FIELDS

_RESULT_PUBLICATION_RULES = {
    "result_id_blank": (
        ("result_id",), "Provide a nonblank result id."
    ),
    "result_summary_blank": (
        ("summary",), "Provide a nonblank result summary."
    ),
    "result_question_duplicate": (
        ("questions",), "Give each result question a unique id."
    ),
    "result_v1_extensions_forbidden": (
        ("contract_version",),
        "Use contract version 2 when observations or limitations are present.",
    ),
    "question_id_blank": (
        ("question_id",), "Provide a nonblank question id."
    ),
    "question_text_blank": (
        ("question",), "Provide a nonblank question."
    ),
    "question_required_reason_missing": (
        ("reason",), "Provide a reason for a question required for the goal."
    ),
    "result_observation_blank": (
        (), "Provide nonblank observations and limitations."
    ),
}


def safe_feedback(text: str, limit: int = 500) -> str:
    """Redact first, then bound public feedback and durable summaries."""
    return " ".join(redact_sensitive_text(text).split())[:limit]


def _identifier(value: str, fallback: str) -> str:
    if not value or len(value) > 100:
        return fallback
    return value if all(char.isalnum() or char in "_-" for char in value) else fallback


def error_type_for(exc: BaseException) -> str:
    """Safe exception type name only; never messages or tracebacks."""
    return _identifier(type(exc).__name__, "unknown")


_ORIGIN_MODULE_PREFIX = "redteam."
_ORIGIN_MODULE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
)
_ORIGIN_LIMIT = 5
_ORIGIN_FRAME_LIMIT = 500


def _cause_chain(exc: BaseException, *, limit: int) -> list[BaseException]:
    """Current exception, then its __cause__ or unsuppressed __context__, bounded by limit."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < limit:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        nxt: BaseException | None = None
        if isinstance(cause, BaseException):
            nxt = cause
        elif isinstance(context, BaseException) and not getattr(
            current, "__suppress_context__", False
        ):
            nxt = context
        current = nxt
    return chain


def exception_chain_for(exc: BaseException, *, limit: int = 5) -> list[str]:
    """Current-first type-name chain, at most `limit` entries (default 5). Type names only:
    no messages, args, tracebacks or traceback objects are copied."""
    return [_identifier(type(item).__name__, "unknown") for item in _cause_chain(exc, limit=limit)]


def _trusted_module_label(module: object) -> str | None:
    """The module __name__ when it is redteam-owned and made of plain name characters."""
    if not isinstance(module, str) or not module.startswith(_ORIGIN_MODULE_PREFIX):
        return None
    if len(module) > 160 or not set(module) <= _ORIGIN_MODULE_CHARS:
        return None
    return module


def _innermost_trusted_frame(tb: TracebackType | None) -> dict[str, Any] | None:
    """Deepest redteam-owned frame among the first 500 traceback frames: module, code name and
    line only. Frames beyond that scan window are ignored."""
    found: dict[str, Any] | None = None
    steps = 0
    while tb is not None and steps < _ORIGIN_FRAME_LIMIT:
        steps += 1
        module = _trusted_module_label(tb.tb_frame.f_globals.get("__name__"))
        line = tb.tb_lineno
        if (module is not None and isinstance(line, int) and not isinstance(line, bool)
                and line > 0):
            found = {
                "module": module,
                "function": _identifier(str(tb.tb_frame.f_code.co_name), "unknown"),
                "line": line,
            }
        tb = tb.tb_next
    return found


def exception_origins_for(
    exc: BaseException, *, limit: int = _ORIGIN_LIMIT,
) -> list[dict[str, Any]]:
    """Innermost redteam-owned raise site per current/cause exception, at most five entries.

    Explicit projection from traceback metadata: validated module name, code object name and
    positive line number. Traceback objects, paths, source text, locals, messages and str(exc)
    are never read into the result. Foreign frames are skipped, and an exception without a
    trusted frame contributes no entry.
    """
    origins: list[dict[str, Any]] = []
    for index, item in enumerate(_cause_chain(exc, limit=min(limit, _ORIGIN_LIMIT))):
        origin = _innermost_trusted_frame(item.__traceback__)
        if origin is not None:
            origins.append({"scope": "current" if index == 0 else "cause", **origin})
    return origins


class ParseErrorContext(BaseModel):
    """Public typed JSON parse coordinates; never source text."""

    coordinate_source: Literal["decoder_input"] = "decoder_input"
    line: int = Field(default=0, ge=0)
    column: int = Field(default=0, ge=0)
    position: int = Field(default=0, ge=0)
    input_length: int = Field(default=0, ge=0)
    syntax_reason: Literal[
        "expected_value", "expected_property_name", "expected_colon", "expected_comma",
        "unterminated_string", "extra_data", "invalid_unicode_escape", "trailing_comma",
        "invalid_control_character", "invalid_escape", "unknown",
    ] = "unknown"
    at_input_end: bool = False
    start_category: Literal["object", "array", "other", "empty"] = "empty"
    first_value_end: int | None = Field(default=None, ge=0)
    remaining_length: int = Field(default=0, ge=0)
    framing: Literal[
        "empty", "first_value_only", "first_value_with_suffix", "undecodable",
    ] = "undecodable"
    suffix_category: Literal[
        "none", "whitespace", "closing_delimiter", "object_start", "array_start", "other",
    ] = "other"


class DecisionDiagnostic(BaseModel):
    code: str
    stage: str
    kind: str
    attempts: int = Field(default=0, ge=0)
    location: str = Field(default="", max_length=200)
    summary: str = Field(max_length=500)
    retryable: bool = False
    cause_scope: Literal[
        "model_input", "target_prerequisite", "policy_wait", "evidence_validity",
        "runtime_contract", "unknown",
    ] = "unknown"
    owner: str = Field(default="", max_length=100)
    repair_condition: str = Field(default="", max_length=500)
    evidence_ids: list[str] = Field(default_factory=list)
    criterion_ids: list[str] = Field(default_factory=list)
    last_cause: DecisionDiagnostic | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    attempt_limit: int | None = None
    stage_attempts: dict[str, int] = Field(default_factory=dict)
    normal_limit: int | None = None
    repair_limit: int | None = None
    transport_limit: int | None = None
    normal_used: int = Field(default=0, ge=0)
    repair_used: int = Field(default=0, ge=0)
    transport_used: int = Field(default=0, ge=0)
    provider_status: int | None = None
    provider_code: str | None = None
    retry_after: float | None = None
    validation_issues: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    termination_reason: Literal["budget_exhausted"] | None = None

    error_type: str = Field(default="", max_length=100)
    exception_chain: list[str] = Field(default_factory=list, max_length=5)
    parse_error: ParseErrorContext | None = None
    failure_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("code", "stage", mode="before")
    @classmethod
    def _safe_labels(cls, value: str) -> str:
        return safe_feedback(value, 100)

    @field_validator("trace", mode="before")
    @classmethod
    def _safe_trace(cls, value):
        return redact_secret_material(value[-20:])

    @field_validator("error_type", mode="before")
    @classmethod
    def _safe_error_type(cls, value: str) -> str:
        if not value:
            return ""
        return _identifier(str(value), "unknown")

    @field_validator("exception_chain", mode="before")
    @classmethod
    def _safe_chain(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value[:5]:
            cleaned.append(_identifier(str(item), "unknown"))
        return cleaned

    @field_validator("failure_counts", mode="before")
    @classmethod
    def _safe_counts(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        cleaned_counts: dict[str, int] = {}
        for key, count in list(value.items())[:50]:
            safe_key = _identifier(str(key), "")
            if not safe_key:
                continue
            if isinstance(count, bool):
                continue
            if not isinstance(count, int) or count < 0:
                continue
            cleaned_counts[safe_key] = count
        return cleaned_counts

    @field_validator("summary", "repair_condition", "owner", mode="before")
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


def shallow_response_shape(value: Any) -> dict[str, Any]:
    """Shallow response type/length only; never raw contents, hashes, or values."""
    if value is None:
        return {"type": "absent", "observed": False}
    shape = _value_shape(value)
    # _value_shape without control_number never copies values; keep safe keys only.
    allowed = {"type", "length", "field_count", "item_types"}
    return {key: shape[key] for key in allowed if key in shape}


class JsonNormalizationContext(BaseModel):
    """Received vs parser-input difference; lengths and flags only, never raw."""

    received_type: Literal[
        "string", "object", "array", "integer", "number", "boolean", "null", "non_json",
    ] = "non_json"
    received_length: int | None = Field(default=None, ge=0)
    parser_input_length: int = Field(default=0, ge=0)
    stripped: bool = False
    fence_stripped: bool = False
    empty: bool = False


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


def json_parse_context(
    exc: json.JSONDecodeError, *, normalization: JsonNormalizationContext | None = None,
) -> dict[str, Any]:
    """Coordinates describe the decoder input, never the original response."""
    context: dict[str, Any] = {
        "proposal_shape": {"observed": False, "reason": "unparsed"},
        "parse_error": parse_error_for(exc).model_dump(mode="json"),
    }
    if normalization is not None:
        context["normalization"] = normalization.model_dump(mode="json")
    return context


def _suffix_category(doc: str, end: int) -> str:
    """Classify chars after the first value without copying them."""
    rest = doc[end:]
    if not rest:
        return "none"
    stripped = rest.lstrip()
    if not stripped:
        return "whitespace"
    first = stripped[:1]
    if first in ("}", "]"):
        return "closing_delimiter"
    if first == "{":
        return "object_start"
    if first == "[":
        return "array_start"
    return "other"


def _first_value_framing(doc: str, pos: int) -> tuple[int | None, int, str, str]:
    """Top-level first-value end, remaining length, framing, and suffix.

    Computed from the decoder input only. A re-parse is used solely for
    framing coordinates and is never accepted as a valid proposal.
    No truncation is asserted from the error position alone.
    """
    input_length = len(doc)
    if not doc.strip():
        return None, input_length, "empty", "none"
    try:
        leading = len(doc) - len(doc.lstrip())
        _, end = json.JSONDecoder().raw_decode(doc, idx=leading)
        remaining = max(0, input_length - end)
        trailing = doc[end:]
        if trailing.strip():
            return end, remaining, "first_value_with_suffix", _suffix_category(doc, end)
        suffix = "whitespace" if trailing else "none"
        return end, remaining, "first_value_only", suffix
    except Exception:
        pass
    return None, input_length, "undecodable", "other"


def parse_error_for(exc: json.JSONDecodeError) -> ParseErrorContext:
    """Typed parse coordinates with safe framing; never source text."""
    first = exc.doc.lstrip()[:1]
    start = "object" if first == "{" else "array" if first == "[" else "other" if first else "empty"
    first_end, remaining, framing, suffix = _first_value_framing(exc.doc, exc.pos)
    return ParseErrorContext.model_validate({
        "coordinate_source": "decoder_input",
        "line": exc.lineno,
        "column": exc.colno,
        "position": exc.pos,
        "input_length": len(exc.doc),
        "syntax_reason": _json_syntax_hint(exc.msg)[0],
        "at_input_end": exc.pos >= len(exc.doc),
        "start_category": start,
        "first_value_end": first_end,
        "remaining_length": remaining,
        "framing": framing,
        "suffix_category": suffix,
    })


_JUDGMENT_LIST_FIELDS = (
    "updates", "questions", "tasks", "reviewed_task_ids", "reviewed_hypothesis_ids",
    "resume_conditions",
)


def _project_declared_judgment(
    view: dict[str, Any], proposal: dict, response_schema: dict | None,
) -> None:
    """Shapes of judgment fields the schema declares; termination only as an enum member."""
    declared = (response_schema or {}).get("properties", {})
    if not isinstance(declared, dict):
        return
    for key in _JUDGMENT_LIST_FIELDS:
        if key in proposal and key in declared:
            view[f"{key}_shape"] = _value_shape(proposal[key])
    if "context_request" in proposal and "context_request" in declared:
        view["context_request_shape"] = _value_shape(proposal["context_request"])
    if "termination" in proposal and isinstance(declared.get("termination"), dict):
        allowed = declared["termination"].get("enum")
        value = proposal["termination"]
        if (
            isinstance(value, str)
            and isinstance(allowed, list)
            and all(isinstance(item, str) for item in allowed)
            and value in allowed
        ):
            view["termination"] = value
        else:
            view["termination_shape"] = _value_shape(value)


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
            _project_declared_judgment(context["proposal"], proposal, response_schema)
        for key in ("known_target_ids", "registered_principal_refs"):
            if isinstance(payload.get(key), list):
                context[f"{key}_shape"] = _value_shape(payload[key])
        for key in ("known_target_count", "registered_principal_count"):
            count = payload.get(key)
            if isinstance(count, int) and not isinstance(count, bool):
                context[f"{key}_shape"] = _value_shape(count, control_number=True)
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
        error_type=error_type_for(exc),
        exception_chain=exception_chain_for(exc),
    )


def diagnostic_for_exception(
    exc: BaseException,
    *,
    stage: str,
    schema: dict[str, Any] | None = None,
) -> DecisionDiagnostic:
    """One safe diagnostic; an owner-supplied schema may publish its fields.

    An optional schema is the input contract the caller just validated against:
    its declared property names replace the shared decision allowlist
    when rendering locations, so nested contract fields stay visible
    while arbitrary keys still cannot. Omitting it keeps the default
    decision-field behavior for existing consumers.
    """
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
            error_type=error_type_for(exc),
            exception_chain=exception_chain_for(exc),
            parse_error=parse_error_for(exc),
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
            error_type=error_type_for(exc),
            exception_chain=exception_chain_for(exc),
        )
    if isinstance(exc, ValidationError):
        # A caller-owned contract may render its own declared field names;
        # without one the shared decision allowlist keeps unknown key names
        # out of every location.
        fields = _schema_fields(schema) if schema is not None else _KNOWN_FIELDS
        # include_input=False is mandatory even though messages are not copied.
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        first: dict[str, Any] = dict(errors[0]) if errors else {}
        issues = [
            {"rule": _identifier(str(error.get("type", "")), "validation"),
             "location": known_location(error.get("loc", ()), fields=fields)}
            for error in errors[:10]
        ]
        action_rules = {
            "unknown_operation": "Use a supported action capability and operation.",
            "tool_outside_capability_namespace": (
                "Choose a tool from the action's capability namespace."
            ),
        }
        rule = str(first.get("type") or "")
        if rule in _RESULT_PUBLICATION_RULES:
            suffix, summary = _RESULT_PUBLICATION_RULES[rule]
            raw_location = tuple(first.get("loc", ()))
            # Pydantic's model validators report the model boundary. Add only
            # code-owned field names; retain structural list indexes from the
            # validation location so caller data never supplies a path label.
            safe_fields = frozenset({
                "result", "questions", "observations", "limitations", "result_id",
                "summary", "question_id", "question", "reason", "contract_version",
            })
            safe_prefix = tuple(
                part for part in raw_location
                if (isinstance(part, int) and not isinstance(part, bool) and part >= 0)
                or (isinstance(part, str) and part in safe_fields)
            )
            owner_path = (
                safe_prefix if rule == "result_observation_blank"
                else (*safe_prefix, *suffix)
            )
            owner_fields = frozenset(part for part in owner_path if isinstance(part, str))
            location = known_location(
                owner_path,
                fields=(fields | owner_fields),
            )
            return DecisionDiagnostic(
                code=rule,
                stage=_identifier(stage, "decision"),
                kind="format",
                location=location,
                summary=summary,
                repair_condition=summary,
                cause_scope="model_input",
                owner="core.contracts.mission",
                retryable=True,
                validation_issues=[{"rule": rule, "location": location}],
                error_type=error_type_for(exc),
                exception_chain=exception_chain_for(exc),
            )
        if rule in action_rules:
            return DecisionDiagnostic(
                code=rule,
                stage=_identifier(stage, "decision"),
                kind="format",
                location=known_location(first.get("loc", ()), fields=fields),
                summary=action_rules[rule],
                retryable=True,
                validation_issues=issues,
                error_type=error_type_for(exc),
                exception_chain=exception_chain_for(exc),
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
            location=known_location(first.get("loc", ()), fields=fields),
            summary=summaries.get(rule, "Match the required model fields and validation rules."),
            retryable=True,
            validation_issues=issues,
            error_type=error_type_for(exc),
            exception_chain=exception_chain_for(exc),
        )
    return DecisionDiagnostic(
        code="invalid_decision",
        stage=_identifier(stage, "decision"),
        kind="format",
        summary="The decision did not satisfy an orchestration validation rule.",
        retryable=True,
        error_type=error_type_for(exc),
        exception_chain=exception_chain_for(exc),
    )


def operational_exception_diagnostic(
    exc: BaseException, stage: str, code: str, summary: str
) -> DecisionDiagnostic:
    """Runner/operation failures; never messages, inputs, or tracebacks."""
    attached = getattr(exc, "diagnostic", None)
    if isinstance(attached, DecisionDiagnostic):
        update: dict[str, object] = {}
        if not attached.error_type:
            update["error_type"] = error_type_for(exc)
        if not attached.exception_chain:
            update["exception_chain"] = exception_chain_for(exc)
        if update:
            return attached.model_copy(update=update)
        return attached
    return DecisionDiagnostic(
        code=_identifier(code, "execution_failed"),
        stage=_identifier(stage, "decision"),
        kind="execution",
        summary=summary,
        retryable=False,
        error_type=error_type_for(exc),
        exception_chain=exception_chain_for(exc),
    )
