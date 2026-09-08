from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class SchemaModel(BaseModel):
    """Strict canonical model used at core boundaries."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TriState(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value)
    raise TypeError(f"value is not canonically serializable: {type(value)!r}")


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *identity_parts: object, length: int = 16) -> str:
    """Build a deterministic ID from semantic identity fields only.

    Callers decide what constitutes identity. Runtime timestamps/randomness must not be passed for
    semantic entities such as relations, requirements, candidates, or contracts.
    """

    payload = canonical_json([str(part) for part in identity_parts])
    digest = sha256_text(payload)[:length]
    return f"{prefix}_{digest}"
