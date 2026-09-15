"""Layout and merge rules for ``GraphNode.properties``.

attrs (predicates) / facts (values) / provenance / retractions, plus the
guard that keeps secrets out of long-lived state.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

ATTRS_KEY = "attrs"
FACTS_KEY = "facts"
PROVENANCE_KEY = "provenance"
RETRACTIONS_KEY = "retractions"
FIRST_SEEN_KEY = "first_seen_step"
LAST_SEEN_KEY = "last_seen_step"

RESERVED_KEYS = frozenset(
    {ATTRS_KEY, FACTS_KEY, PROVENANCE_KEY, RETRACTIONS_KEY, FIRST_SEEN_KEY, LAST_SEEN_KEY}
)

#: Key names that must never appear in ``attrs``/``facts``.
_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "cvv",
        "cvc",
        "pan",
        "card_number",
        "token",
        "access_token",
        "refresh_token",
        "session_cookie",
        "api_key",
        "apikey",
        "private_key",
        "ssn",
        "pin",
        "otp",
    }
)

#: A long digit run — PAN/account-number shaped. Rejected as a value.
_LONG_DIGIT_RUN = re.compile(r"^\d{9,}$")

AttrValue = bool | str | int


class SensitiveValueError(ValueError):
    """Raised when node properties carry something that must not be persisted."""


class Confidence(str, Enum):
    """How much weight an observation carries. Weakest grade by default."""

    MEASURED = "measured"
    SERVER_DECLARED = "server_declared"
    ASSERTED = "asserted"
    INFERRED = "inferred"


_STRONG = frozenset({Confidence.MEASURED, Confidence.SERVER_DECLARED})


class Provenance(BaseModel):
    """Where one observation came from. Attached to nodes and edges."""

    step: int = 0
    source: str = ""
    evidence_id: str | None = None
    run_id: str | None = None
    confidence: Confidence = Confidence.INFERRED

    @property
    def is_strong(self) -> bool:
        return self.confidence in _STRONG


def _reject_secret(container: str, key: str, value: Any) -> None:
    if key.lower() in _SECRET_KEYS:
        raise SensitiveValueError(
            f"{container}[{key!r}] is a secret-shaped key — the Attack Graph stores "
            f"references and predicates, never credentials"
        )
    if isinstance(value, str) and _LONG_DIGIT_RUN.match(value):
        raise SensitiveValueError(
            f"{container}[{key!r}] looks like a PAN/account number — store a reference instead"
        )


def validate_attrs(attrs: dict[str, Any]) -> dict[str, AttrValue]:
    """Predicates only: ``bool``/``str``/``int``, no secrets."""
    validated: dict[str, AttrValue] = {}
    for key, value in attrs.items():
        _reject_secret(ATTRS_KEY, key, value)
        if not isinstance(value, (bool, str, int)):
            raise SensitiveValueError(
                f"attrs[{key!r}] is {type(value).__name__}; attrs hold predicates "
                f"(bool/str/int) — put measurements in facts"
            )
        validated[key] = value
    return validated


def validate_facts(facts: dict[str, Any]) -> dict[str, Any]:
    for key, value in facts.items():
        _reject_secret(FACTS_KEY, key, value)
    return dict(facts)


def build_properties(
    *,
    attrs: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
    step: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a well-formed ``GraphNode.properties``."""
    properties: dict[str, Any] = {
        ATTRS_KEY: validate_attrs(attrs or {}),
        FACTS_KEY: validate_facts(facts or {}),
        PROVENANCE_KEY: [provenance.model_dump(mode="json")] if provenance else [],
        RETRACTIONS_KEY: [],
        FIRST_SEEN_KEY: step,
        LAST_SEEN_KEY: step,
    }
    for key, value in (extra or {}).items():
        if key in RESERVED_KEYS:
            raise ValueError(f"{key!r} is a reserved property key — pass it via attrs/facts")
        properties[key] = value
    return properties


def attrs_of(properties: dict[str, Any]) -> dict[str, AttrValue]:
    value = properties.get(ATTRS_KEY)
    return dict(value) if isinstance(value, dict) else {}


def facts_of(properties: dict[str, Any]) -> dict[str, Any]:
    value = properties.get(FACTS_KEY)
    return dict(value) if isinstance(value, dict) else {}


def provenance_of(properties: dict[str, Any]) -> list[dict[str, Any]]:
    value = properties.get(PROVENANCE_KEY)
    return list(value) if isinstance(value, list) else []


def retractions_of(properties: dict[str, Any]) -> list[dict[str, Any]]:
    value = properties.get(RETRACTIONS_KEY)
    return list(value) if isinstance(value, list) else []


def last_seen_step(properties: dict[str, Any]) -> int:
    value = properties.get(LAST_SEEN_KEY, 0)
    return value if isinstance(value, int) else 0


def first_seen_step(properties: dict[str, Any]) -> int:
    value = properties.get(FIRST_SEEN_KEY, 0)
    return value if isinstance(value, int) else 0


def is_stale(properties: dict[str, Any], current_step: int, *, max_age: int) -> bool:
    """``max_age <= 0`` disables the check."""
    return max_age > 0 and (current_step - last_seen_step(properties)) > max_age


def _merge_attrs(
    existing: dict[str, AttrValue], incoming: dict[str, AttrValue]
) -> dict[str, AttrValue]:
    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        # Sticky True: something observed once stays observed until it is
        # explicitly retracted with a reason.
        if isinstance(current, bool) and isinstance(value, bool):
            merged[key] = current or value
        else:
            merged[key] = value
    return merged


def _merge_facts(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue  # an unobserved field never erases an observed one
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = current + [item for item in value if item not in current]
        else:
            merged[key] = value
    return merged


def merge_properties(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold a fresh observation into what the graph already holds.

    Never destructive: sticky ``True`` attrs, list union, incoming
    ``None`` ignored, ``last_seen_step`` moves forward only.
    """
    merged: dict[str, Any] = {
        key: value
        for key, value in existing.items()
        if key not in RESERVED_KEYS
    }
    for key, value in incoming.items():
        if key not in RESERVED_KEYS:
            merged[key] = value

    merged[ATTRS_KEY] = _merge_attrs(attrs_of(existing), attrs_of(incoming))
    merged[FACTS_KEY] = _merge_facts(facts_of(existing), facts_of(incoming))

    provenance = provenance_of(existing)
    provenance.extend(item for item in provenance_of(incoming) if item not in provenance)
    merged[PROVENANCE_KEY] = provenance

    retractions = retractions_of(existing)
    retractions.extend(item for item in retractions_of(incoming) if item not in retractions)
    merged[RETRACTIONS_KEY] = retractions

    merged[FIRST_SEEN_KEY] = min(first_seen_step(existing), first_seen_step(incoming))
    merged[LAST_SEEN_KEY] = max(last_seen_step(existing), last_seen_step(incoming))
    return merged


def apply_retraction(
    properties: dict[str, Any], key: str, *, step: int, reason: str
) -> tuple[dict[str, Any], bool]:
    """Withdraw one predicate, recording why. ``reason`` is required.

    Returns the new properties and whether anything was withdrawn.
    """
    if not reason:
        raise ValueError("a retraction needs a reason — otherwise the state change is untraceable")

    attrs = attrs_of(properties)
    if key not in attrs:
        return properties, False

    attrs.pop(key)
    retractions = retractions_of(properties)
    retractions.append({"step": step, "key": key, "reason": reason})

    updated = dict(properties)
    updated[ATTRS_KEY] = attrs
    updated[RETRACTIONS_KEY] = retractions
    return updated, True


def predicate_flags(properties: dict[str, Any]) -> str:
    """Compact rendering of the true predicates, e.g. ``"+reachable+tls"``."""
    return "".join(
        f"+{key}" for key, value in sorted(attrs_of(properties).items()) if value is True
    )


class NodeSummary(BaseModel):
    """One line about a node, as shown to a planner."""

    node_id: str
    key: str
    flags: str = ""
    labels: list[str] = Field(default_factory=list)
