from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, field_validator

from redteam_core.models.base import SchemaModel


class GuardEffect(StrEnum):
    BLOCKS = "BLOCKS"
    MAY_BLOCK = "MAY_BLOCK"
    INFORMATIONAL = "INFORMATIONAL"


class GuardScope(StrEnum):
    SOURCE = "SOURCE"
    TARGET = "TARGET"


class RelationSemantics(SchemaModel):
    """Domain-supplied graph meaning for one registered relation type.

    The core traversal engine consumes only this structural vocabulary. Concrete relation names
    remain owned by bundles and never appear in Frontier code.
    """

    traversable: bool = False
    source_tags: tuple[str, ...] = ()
    mechanism_tags: tuple[str, ...] = ()
    sink_tags: tuple[str, ...] = ()
    guard_effect: GuardEffect | None = None
    guard_scope: GuardScope = GuardScope.TARGET
    context_bridge: bool = False
    proves_influence: bool = False
    continue_after_sink: bool = False

    @field_validator("source_tags", "mechanism_tags", "sink_tags")
    @classmethod
    def validate_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("relation semantic tags must be unique")
        for value in values:
            if not value or len(value) > 120:
                raise ValueError("relation semantic tag must be non-empty and <=120 chars")
        return values
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)
