"""Minimal proposal input and runtime hydration, separate from stored path models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from redteam.orchestration.paths.models import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    PathPlan,
    PathStep,
    PathValidationError,
)

_REQUIRED_FIELDS = ("description",)
_HYPOTHESIS_KINDS = frozenset({"hypothesis_confirmed", "hypothesis_refuted"})
_PATH_RUNTIME_FIELDS = frozenset(
    {
        "path_id", "run_id", "mission_id", "mission_task_id", "version", "parent_version",
        "status", "goal", "review_records", "changed_step_ids", "evidence_ids",
        "planning_artifacts",
    }
)


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


def writing_schema(
    model: type[BaseModel], *, actions: Iterable[tuple[str, str]] | None = None,
    selected_tool: bool = False,
) -> dict[str, Any]:
    """Expose meaning and structural references without operational boilerplate.

    Uses basic JSON Schema constraints supported by structured-output providers.
    The hypothesis-reference relationship and whitespace checks run in code.
    """
    schema = deepcopy(model.model_json_schema())
    defs = schema.get("$defs", {})
    criterion_schema = schema if model is CompletionCriterion else defs.get("CompletionCriterion")
    if criterion_schema is not None:
        properties = criterion_schema["properties"]
        properties["kind"].update(enum=sorted(ALLOWED_CRITERION_KINDS))
        properties["kind"]["default"] = "semantic"
        properties["description"].update(minLength=1)
        properties["min_evidence"].update(minimum=1)
        for name in _REQUIRED_FIELDS:
            properties[name].pop("default", None)
        criterion_schema["required"] = list(
            dict.fromkeys([*criterion_schema.get("required", []), *_REQUIRED_FIELDS])
        )
    path_schema = schema if model is PathPlan else defs.get("PathPlan")
    if path_schema is not None:
        path_schema["required"] = [
            name for name in path_schema.get("required", []) if name not in _PATH_RUNTIME_FIELDS
        ]
        for name in _PATH_RUNTIME_FIELDS:
            path_schema.get("properties", {}).pop(name, None)
    step_schema = schema if model is PathStep else defs.get("PathStep")
    if step_schema is not None:
        criterion_item = {
            "anyOf": [{"$ref": "#/$defs/CompletionCriterion"}, {"type": "string", "minLength": 1}]
        }
        step_schema["properties"]["completion_criteria"] = {
            "anyOf": [
                {"type": "array", "items": criterion_item},
                *criterion_item["anyOf"],
            ]
        }
    # Syntax is stable; available semantics come from the selected catalog.
    # Omitting a catalog is not an implicit builtin-only execution mode.
    pairs = tuple(actions) if actions is not None else ()
    action_schema = (
        schema if selected_tool and "tool_id" in schema.get("properties", {})
        else defs.get("ActionIntent")
    )
    if action_schema is not None:
        action_properties = action_schema.get("properties", {})
        if selected_tool:
            # Direct calls are owned by the selected tool. Older models may
            # still echo capability/operation labels; accept those optional
            # hints without using them as an execution binding. Keep
            # binding_mode code-owned so model output cannot switch paths.
            action_properties.pop("binding_mode", None)
            action_schema["required"] = [
                name for name in action_schema.get("required", [])
                if name not in {"capability", "operation", "binding_mode"}
            ]
        if not selected_tool and "capability" in action_properties:
            action_properties["capability"].update(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
            if pairs:
                action_properties["capability"].update(enum=sorted({cap for cap, _ in pairs}))
        if not selected_tool and "operation" in action_properties:
            action_properties["operation"].update(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
            if pairs:
                action_properties["operation"].update(enum=sorted({op for _, op in pairs}))
    return schema


def _validate_criterion(value: Any, location: str) -> None:
    if isinstance(value, CompletionCriterion):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        if value.strip():
            return
        value = {"description": value}
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
    kind = value.get("kind", "semantic")
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
    count = value.get("min_evidence", 1)
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
    """Validate the meaning-bearing input without requiring repeated defaults.

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
            if isinstance(criteria, (str, dict, CompletionCriterion)):
                criteria = [criteria]
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


def normalize_path_proposals(
    raw: dict[str, Any], *, defaults: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Hydrate only defined path slots before validating their storage models.

    Model-provided operational values are replaced when runtime defaults exist.
    Tool arguments and arbitrary nested objects are never traversed.
    """
    normalized = deepcopy(raw)

    def visit(candidate: dict[str, Any]) -> None:
        steps = candidate.get("steps")
        if isinstance(steps, list):
            if defaults is not None:
                candidate.update(
                    {key: value for key, value in defaults.items() if key in _PATH_RUNTIME_FIELDS}
                )
            for step in steps:
                if not isinstance(step, dict):
                    continue
                criteria = step.get("completion_criteria")
                if criteria is None:
                    description = step.get("objective")
                    criteria = (
                        [description]
                        if isinstance(description, str) and description.strip()
                        else []
                    )
                elif isinstance(criteria, (str, dict, CompletionCriterion)):
                    criteria = [criteria]
                if isinstance(criteria, list):
                    prepared = []
                    for criterion in criteria:
                        if isinstance(criterion, CompletionCriterion):
                            criterion = criterion.model_dump(mode="json")
                        if isinstance(criterion, str):
                            criterion = {"description": criterion}
                        if isinstance(criterion, dict):
                            criterion = {"kind": "semantic", "min_evidence": 1, **criterion}
                        prepared.append(criterion)
                    step["completion_criteria"] = prepared
                step["status"], step["outcome"], step["evidence_ids"] = "pending", None, []
        nested = candidate.get("path")
        if isinstance(nested, dict):
            visit(nested)

    visit(normalized)
    return normalized
