"""Strict creation rules, separate from backwards-compatible stored path models."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from redteam.orchestration.paths.models import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    PathValidationError,
)

_REQUIRED_FIELDS = ("kind", "description", "min_evidence")
_HYPOTHESIS_KINDS = frozenset({"hypothesis_confirmed", "hypothesis_refuted"})


class CriterionWritingError(PathValidationError, ValueError):
    """A correction request containing rule metadata, never rejected input values."""

    def __init__(
        self,
        code: str,
        location: str,
        description: str,
        required_fields: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.location = location[:200]
        self.required_fields = required_fields
        self.allowed_kinds = tuple(sorted(ALLOWED_CRITERION_KINDS))
        self.description = description
        super().__init__(f"{code} at {self.location}: {description}")

    def correction(self) -> dict[str, Any]:
        """Safe feedback for another model attempt within the existing decision budget."""
        return {
            "code": self.code,
            "location": self.location,
            "required_fields": list(self.required_fields),
            "allowed_kinds": list(self.allowed_kinds),
            "rule": self.description,
        }


def writing_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Require explicit supported criteria without narrowing the storage schema.

    Uses basic JSON Schema constraints supported by structured-output providers.
    The hypothesis-reference relationship and whitespace checks run in code.
    """
    from redteam.core.action_semantics import CANONICAL_CAPABILITIES, CANONICAL_OPERATIONS

    schema = deepcopy(model.model_json_schema())
    defs = schema.get("$defs", {})
    criterion_schema = schema if model is CompletionCriterion else defs.get("CompletionCriterion")
    if criterion_schema is not None:
        properties = criterion_schema["properties"]
        properties["kind"].update(enum=sorted(ALLOWED_CRITERION_KINDS))
        properties["description"].update(minLength=1)
        properties["min_evidence"].update(minimum=1)
        for name in _REQUIRED_FIELDS:
            properties[name].pop("default", None)
        criterion_schema["required"] = list(
            dict.fromkeys([*criterion_schema.get("required", []), *_REQUIRED_FIELDS])
        )
    # Constrain the action vocabulary at generation time so the model cannot
    # emit a malformed operation (e.g. a ToolId or a capability.operation key in
    # the bare operation field) -- the same canonical names the validator checks.
    action_schema = defs.get("ActionIntent")
    if action_schema is not None:
        action_properties = action_schema.get("properties", {})
        if "capability" in action_properties:
            action_properties["capability"].update(enum=sorted(CANONICAL_CAPABILITIES))
        if "operation" in action_properties:
            action_properties["operation"].update(enum=sorted(CANONICAL_OPERATIONS))
    return schema


def _validate_criterion(value: Any, location: str) -> None:
    if not isinstance(value, dict):
        raise CriterionWritingError(
            "criterion_object_required",
            location,
            "Write a structured criterion object.",
            _REQUIRED_FIELDS,
        )
    missing = tuple(name for name in _REQUIRED_FIELDS if name not in value)
    if missing:
        raise CriterionWritingError(
            "criterion_fields_required", location, "Write all required criterion fields.", missing
        )
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in ALLOWED_CRITERION_KINDS:
        raise CriterionWritingError(
            "criterion_kind_unsupported",
            location + ".kind",
            "Choose one of the supported criterion kinds.",
            ("kind",),
        )
    description = value["description"]
    if not isinstance(description, str) or not description.strip():
        raise CriterionWritingError(
            "criterion_description_required",
            location + ".description",
            "Write a nonblank natural-language criterion description.",
            ("description",),
        )
    count = value["min_evidence"]
    if type(count) is not int or count < 1:
        raise CriterionWritingError(
            "criterion_evidence_count_invalid",
            location + ".min_evidence",
            "Write an integer evidence requirement of at least one.",
            ("min_evidence",),
        )
    reference = value.get("hypothesis_id")
    if kind in _HYPOTHESIS_KINDS and (not isinstance(reference, str) or not reference.strip()):
        raise CriterionWritingError(
            "criterion_hypothesis_reference_required",
            location + ".hypothesis_id",
            "Hypothesis criteria require a nonblank hypothesis reference.",
            ("hypothesis_id",),
        )


def validate_written_criteria(raw: dict[str, Any]) -> None:
    """Validate raw path candidates before Pydantic can normalize or add defaults.

    Only visit defined path fields. Tool arguments, model reasons and other free
    text remain data; arbitrary object keys never become error locations.
    Structural checks and hypothesis/evidence relationships remain the domain
    validator's responsibility.
    """

    def visit(candidate: Any, location: str) -> None:
        if not isinstance(candidate, dict):
            return
        if "completion_criteria" in candidate:
            criteria = candidate["completion_criteria"]
            if not isinstance(criteria, list):
                raise CriterionWritingError(
                    "criterion_list_required",
                    location + ".completion_criteria",
                    "Write completion criteria as an array of structured objects.",
                )
            for index, criterion in enumerate(criteria):
                _validate_criterion(criterion, f"{location}.completion_criteria[{index}]")
        steps = candidate.get("steps")
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                visit(step, f"{location}.steps[{index}]")
        if isinstance(candidate.get("path"), dict):
            visit(candidate["path"], location + ".path")

    visit(raw, "$")
