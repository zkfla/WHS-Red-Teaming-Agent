"""Layout and merge rules for ``GraphNode.properties``.

attrs (predicates) / facts (values) / provenance / retractions / cleanup,
plus the guard that keeps secrets out. Docs: ``docs/agent/attack-graph.md``.
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
CLEANUP_KEY = "cleanup"

RESERVED_KEYS = frozenset(
    {
        ATTRS_KEY,
        FACTS_KEY,
        PROVENANCE_KEY,
        RETRACTIONS_KEY,
        FIRST_SEEN_KEY,
        LAST_SEEN_KEY,
        CLEANUP_KEY,
    }
)

#: Cleanup status values, ordered so a merge can never move backwards.
CLEANUP_PENDING = "pending"
CLEANUP_CLEANED = "cleaned"
CLEANUP_FAILED = "failed"
CLEANUP_NOT_REQUIRED = "not_required"

_CLEANUP_RANK = {
    CLEANUP_PENDING: 0,
    CLEANUP_FAILED: 1,
    CLEANUP_NOT_REQUIRED: 2,
    CLEANUP_CLEANED: 3,
}

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

#: Matched with ``_`` boundaries, so ``admin_password`` is caught and
#: ``spinning`` is not.
_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:" + "|".join(sorted(_SECRET_KEYS, key=len, reverse=True)) + r")(?:_|$)"
)
_NON_WORD = re.compile(r"[^a-z0-9]+")

#: PAN/account-number shaped, as a string or an integer.
_LONG_DIGIT_RUN = re.compile(r"^\d{9,}$")
_PAN_MIN_DIGITS = 9

#: Sliding window of provenance per node/edge; the first entry is always
#: kept so "who first saw this" survives.
MAX_PROVENANCE_ENTRIES = 20

#: Items kept in a list-valued fact. Rewriting facts is O(size), so an
#: unbounded list makes a long run quadratic. Newest kept; the Evidence
#: Store holds the complete record.
MAX_FACT_LIST_ITEMS = 200

#: A pattern can never ask for "".
_BLANK_KEY = re.compile(r"^\s*$")

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


def _looks_secret(key: str) -> bool:
    normalised = _NON_WORD.sub("_", key.lower()).strip("_")
    return bool(_SECRET_KEY_PATTERN.search(normalised))


def _looks_like_pan(value: Any) -> bool:
    if isinstance(value, bool):  # bool is an int subclass; True is not a PAN
        return False
    if isinstance(value, str):
        return bool(_LONG_DIGIT_RUN.match(value))
    if isinstance(value, int):
        return len(str(abs(value))) >= _PAN_MIN_DIGITS
    return False


def _reject_secret(container: str, key: str, value: Any) -> None:
    if _BLANK_KEY.match(key):
        raise ValueError(f"{container} has a blank key — nothing can ever match it")
    if any(ch < " " or ch == "\x7f" for ch in key):
        raise ValueError(
            f"{container}[{key!r}] contains a control character; it would corrupt logs "
            f"and the JSONL snapshot"
        )
    if _looks_secret(key):
        raise SensitiveValueError(
            f"{container}[{key!r}] is a secret-shaped key — the Attack Graph stores "
            f"references and predicates, never credentials"
        )
    if _looks_like_pan(value):
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
    cleanup: dict[str, Any] | None = None,
    step: int | None = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a well-formed ``GraphNode.properties``.

    ``step=None`` means "no step to record" — distinct from step 0, which
    is a real first turn.
    """
    recorded_step = 0 if step is None else step
    properties: dict[str, Any] = {
        ATTRS_KEY: validate_attrs(attrs or {}),
        FACTS_KEY: validate_facts(facts or {}),
        PROVENANCE_KEY: [provenance.model_dump(mode="json")] if provenance else [],
        RETRACTIONS_KEY: [],
        FIRST_SEEN_KEY: recorded_step,
        LAST_SEEN_KEY: recorded_step,
    }
    if cleanup is not None:
        properties[CLEANUP_KEY] = dict(cleanup)
    for key, value in (extra or {}).items():
        if key in RESERVED_KEYS:
            raise ValueError(f"{key!r} is a reserved property key — pass it via attrs/facts")
        _reject_secret("properties", key, value)
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


def cleanup_of(properties: dict[str, Any]) -> dict[str, Any] | None:
    value = properties.get(CLEANUP_KEY)
    return dict(value) if isinstance(value, dict) else None


def last_seen_step(properties: dict[str, Any]) -> int:
    value = properties.get(LAST_SEEN_KEY, 0)
    return value if isinstance(value, int) else 0


def first_seen_step(properties: dict[str, Any]) -> int:
    value = properties.get(FIRST_SEEN_KEY, 0)
    return value if isinstance(value, int) else 0


def is_stale(properties: dict[str, Any], current_step: int, *, max_age: int) -> bool:
    """``max_age <= 0`` disables the check."""
    return max_age > 0 and (current_step - last_seen_step(properties)) > max_age


def _retracted_at(retractions: list[dict[str, Any]]) -> dict[str, int]:
    """Newest retraction step per attr."""
    latest: dict[str, int] = {}
    for entry in retractions:
        key = str(entry.get("key", ""))
        step = entry.get("step", 0)
        if isinstance(step, int):
            latest[key] = max(latest.get(key, step), step)
    return latest


def _merge_attrs(
    existing: dict[str, AttrValue],
    incoming: dict[str, AttrValue],
    *,
    retracted_at: dict[str, int] | None = None,
    incoming_step: int = 0,
) -> dict[str, AttrValue]:
    merged = dict(existing)
    blocked = retracted_at or {}
    for key, value in incoming.items():
        # A withdrawn predicate must not come back from an *older*
        # observation (a stale snapshot import). A newer one still restores
        # it — that is what re-observing is for.
        if key in blocked and incoming_step <= blocked[key]:
            continue
        current = merged.get(key)
        # Sticky True: something observed once stays observed until it is
        # explicitly retracted with a reason.
        if isinstance(current, bool) and isinstance(value, bool):
            merged[key] = current or value
        else:
            merged[key] = value
    return merged


def _merge_cleanup(
    existing: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Cleanup status only ever moves forward — a later observation must not
    reset an already-cleaned resource to "pending"."""
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    old = _CLEANUP_RANK.get(str(existing.get("status")), 0)
    new = _CLEANUP_RANK.get(str(incoming.get("status")), 0)
    return {**existing, **incoming} if new >= old else {**incoming, **existing}


def _union(current: list[Any], incoming: list[Any], *, limit: int | None = None) -> list[Any]:
    """Order-preserving union, optionally bounded to the newest ``limit``.

    Set-based rather than the quadratic ``item not in current`` scan.
    """
    try:
        seen = set(current)
        added = []
        for item in incoming:
            if item not in seen:
                seen.add(item)
                added.append(item)
        merged = current + added
    except TypeError:  # unhashable items — fall back to the scan
        merged = current + [item for item in incoming if item not in current]
    if limit is not None and len(merged) > limit:
        return merged[-limit:]
    return merged


def _merge_facts(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue  # an unobserved field never erases an observed one
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = _union(current, value, limit=MAX_FACT_LIST_ITEMS)
        else:
            merged[key] = value
    return merged


def merge_properties(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold a fresh observation into what the graph already holds.

    Never destructive: sticky ``True`` attrs, list union, incoming ``None``
    ignored, ``last_seen_step`` moves forward only.
    """
    merged: dict[str, Any] = {
        key: value
        for key, value in existing.items()
        if key not in RESERVED_KEYS
    }
    for key, value in incoming.items():
        if key not in RESERVED_KEYS:
            merged[key] = value

    retractions = _union(retractions_of(existing), retractions_of(incoming))
    merged[RETRACTIONS_KEY] = retractions

    merged[ATTRS_KEY] = _merge_attrs(
        attrs_of(existing),
        attrs_of(incoming),
        retracted_at=_retracted_at(retractions),
        incoming_step=last_seen_step(incoming),
    )
    merged[FACTS_KEY] = _merge_facts(facts_of(existing), facts_of(incoming))
    merged[PROVENANCE_KEY] = _cap_provenance(
        _union(provenance_of(existing), provenance_of(incoming))
    )

    cleanup = _merge_cleanup(cleanup_of(existing), cleanup_of(incoming))
    if cleanup is not None:
        merged[CLEANUP_KEY] = cleanup

    merged[FIRST_SEEN_KEY] = min(first_seen_step(existing), first_seen_step(incoming))
    merged[LAST_SEEN_KEY] = max(last_seen_step(existing), last_seen_step(incoming))
    return merged


def _cap_provenance(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first entry and the most recent window of the rest."""
    if len(entries) <= MAX_PROVENANCE_ENTRIES:
        return entries
    return [entries[0], *entries[-(MAX_PROVENANCE_ENTRIES - 1) :]]


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
