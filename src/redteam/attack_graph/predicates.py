"""Layout and merge rules for ``GraphNode.properties``.

attrs (predicates) / facts (values) / provenance / retractions / cleanup,
plus the guard that keeps secrets out. Docs: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ATTRS_KEY = "attrs"
FACTS_KEY = "facts"
PROVENANCE_KEY = "provenance"
RETRACTIONS_KEY = "retractions"
FIRST_SEEN_KEY = "first_seen_step"
LAST_SEEN_KEY = "last_seen_step"
#: Row version advanced by every applied row update; orders retractions
#: and re-observations across workers sharing one graph.
OBSERVATION_VERSION_KEY = "observation_version"
#: Row version stamped on a claim entry when its value or source changes.
ASSERTED_VERSION_KEY = "asserted_version"
CLEANUP_KEY = "cleanup"
#: Step at which each predicate was last asserted. Node-level
#: ``last_seen_step`` moves whenever *anything* about the node is written,
#: so using it to age a predicate means an unrelated note or attempt log
#: silently renews a belief nobody re-checked.
ATTRS_SEEN_KEY = "attrs_observed_at"

#: Per-claim write source. Node-level ``provenance`` records that *something*
#: about the node was written; it cannot say which predicate or value came
#: from which run. ``claim_provenance`` maps one entry per asserted claim.
CLAIM_PROVENANCE_KEY = "claim_provenance"
CLAIM_ATTR_PREFIX = "attrs:"
CLAIM_FACT_PREFIX = "facts:"

RESERVED_KEYS = frozenset(
    {
        ATTRS_KEY,
        FACTS_KEY,
        PROVENANCE_KEY,
        RETRACTIONS_KEY,
        FIRST_SEEN_KEY,
        LAST_SEEN_KEY,
        OBSERVATION_VERSION_KEY,
        CLEANUP_KEY,
        ATTRS_SEEN_KEY,
        CLAIM_PROVENANCE_KEY,
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

#: How far to walk into nested values looking for secrets. Anything deeper
#: is refused rather than stored unscanned.
_MAX_SCAN_DEPTH = 6

#: PAN/account-number shaped, as a string or an integer.
_LONG_DIGIT_RUN = re.compile(r"^\d{9,}$")
_PAN_MIN_DIGITS = 9

#: Sliding window of provenance per node/edge; the first entry is always
#: kept so "who first saw this" survives.
MAX_PROVENANCE_ENTRIES = 20

#: Items kept in a *capped* list-valued fact. Rewriting facts is O(size),
#: so an unbounded log makes a long run quadratic. Newest kept.
MAX_FACT_LIST_ITEMS = 200

#: Nothing is truncated. A fact list is either a link something depends on
#: (``evidence_ids``) or a record something is decided from
#: (``attempted_operations`` — dropping the oldest re-runs work already
#: done). Past this many items a single list is a bug signal, not a
#: capacity problem, so it is reported rather than silently trimmed.
CAPPED_LIST_FACTS: frozenset[str] = frozenset()
WARN_FACT_LIST_ITEMS = 5_000

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


def _scan_nested(container: str, path: str, value: Any, depth: int = 0) -> None:
    """Walk into dicts/lists — a secret one level down is still a secret."""
    if depth > _MAX_SCAN_DEPTH:
        raise SensitiveValueError(
            f"{container}[{path}] nests deeper than {_MAX_SCAN_DEPTH}; it cannot be "
            f"scanned for secrets, so it must not be stored"
        )
    if isinstance(value, dict):
        for inner_key, inner in value.items():
            _reject_secret(container, str(inner_key), inner, path=f"{path}.{inner_key}")
            _scan_nested(container, f"{path}.{inner_key}", inner, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for index, inner in enumerate(value):
            if _looks_like_pan(inner):
                raise SensitiveValueError(
                    f"{container}[{path}[{index}]] looks like a PAN/account number"
                )
            _scan_nested(container, f"{path}[{index}]", inner, depth + 1)


def _reject_secret(container: str, key: str, value: Any, path: str | None = None) -> None:
    path = path or key
    if _BLANK_KEY.match(key):
        raise ValueError(f"{container} has a blank key — nothing can ever match it")
    if any(ch < " " or ch == "\x7f" for ch in key):
        raise ValueError(
            f"{container}[{key!r}] contains a control character; it would corrupt logs "
            f"and the JSONL snapshot"
        )
    if _looks_secret(key):
        raise SensitiveValueError(
            f"{container}[{path!r}] is a secret-shaped key — the Attack Graph stores "
            f"references and predicates, never credentials"
        )
    if _looks_like_pan(value):
        raise SensitiveValueError(
            f"{container}[{path!r}] looks like a PAN/account number — store a reference instead"
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
        # Facts hold arbitrary observed values, so a credential can arrive
        # nested inside one (``metadata: {"password": ...}``) and used to
        # sail straight through into snapshots and the rendered report.
        _scan_nested(FACTS_KEY, key, value)
    return dict(facts)


def validate_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Scan a whole ``properties`` mapping, whatever route it arrived by.

    ``validate_attrs``/``validate_facts`` only cover what goes through
    ``build_properties``. Custom top-level keys, edge properties and
    imported snapshots reach the store without passing either.
    """
    for key, value in properties.items():
        if key in (PROVENANCE_KEY, RETRACTIONS_KEY, ATTRS_SEEN_KEY):
            continue  # code-generated bookkeeping, not observed values
        if key == CLAIM_PROVENANCE_KEY:
            _validate_claim_provenance(value)
            continue  # per-claim write source, likewise bookkeeping
        if key == ATTRS_KEY and isinstance(value, dict):
            validate_attrs(value)
            continue
        if key == FACTS_KEY and isinstance(value, dict):
            validate_facts(value)
            continue
        if key in (FIRST_SEEN_KEY, LAST_SEEN_KEY, OBSERVATION_VERSION_KEY, CLEANUP_KEY):
            continue
        _reject_secret("properties", key, value)
        _scan_nested("properties", key, value)
    return properties


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
    validated_attrs = validate_attrs(attrs or {})
    validated_facts = validate_facts(facts or {})
    properties: dict[str, Any] = {
        ATTRS_KEY: validated_attrs,
        ATTRS_SEEN_KEY: {key: recorded_step for key in validated_attrs},
        FACTS_KEY: validated_facts,
        PROVENANCE_KEY: [provenance.model_dump(mode="json")] if provenance else [],
        RETRACTIONS_KEY: [],
        FIRST_SEEN_KEY: recorded_step,
        LAST_SEEN_KEY: recorded_step,
        CLAIM_PROVENANCE_KEY: _claim_entries_for_write(
            attrs=validated_attrs,
            facts=validated_facts,
            provenance=provenance,
            step=recorded_step,
        ),
    }
    if cleanup is not None:
        properties[CLEANUP_KEY] = dict(cleanup)
    for key, value in (extra or {}).items():
        if key in RESERVED_KEYS:
            raise ValueError(f"{key!r} is a reserved property key — pass it via attrs/facts")
        _reject_secret("properties", key, value)
        _scan_nested("properties", key, value)
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


def claim_key_for_attr(key: str) -> str:
    """Claim-map key for one predicate, e.g. ``"attrs:reachable"``."""
    return f"{CLAIM_ATTR_PREFIX}{key}"


def claim_key_for_fact(key: str) -> str:
    """Claim-map key for one fact value, e.g. ``"facts:status_code"``."""
    return f"{CLAIM_FACT_PREFIX}{key}"


def claim_provenance_of(properties: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-claim write source (``attrs:<key>`` / ``facts:<key>`` entries).

    Unknown to older readers, which keep working: they only look up the
    coarse ``attrs`` / ``facts`` / ``provenance`` keys. Entries carry
    ``source``, ``evidence_id`` (alias ``evidence``), ``run_id`` (alias
    ``run``), ``confidence`` and ``step`` of the write that produced them.
    """
    value = properties.get(CLAIM_PROVENANCE_KEY)
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for claim_key, entry in value.items():
        if isinstance(entry, dict):
            out[str(claim_key)] = dict(entry)
    return out


def _validate_claim_provenance(value: Any) -> dict[str, dict[str, Any]]:
    """Structural check for the claim map; secrets cannot hide here."""
    if not isinstance(value, dict):
        raise ValueError("claim_provenance must map claim keys to entries")
    for claim_key, entry in value.items():
        key = str(claim_key)
        if not (key.startswith(CLAIM_ATTR_PREFIX) or key.startswith(CLAIM_FACT_PREFIX)):
            raise ValueError(f"claim_provenance has an unknown claim key {key!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"claim_provenance[{key!r}] must be a mapping")
        _reject_secret("claim_provenance", key, "")
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)}


def _snapshot_value(value: Any) -> Any:
    """JSON-safe copy of one asserted value for the claim map."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _snapshot_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_snapshot_value(v) for v in value), key=str)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001 - fall through to str()
            pass
    if hasattr(value, "value"):
        try:
            return _snapshot_value(value.value)
        except Exception:  # noqa: BLE001 - fall through to str()
            pass
    return str(value)


def _canonical_value(value: Any) -> str:
    """Stable string for value-equality checks (sticky merge, projection)."""
    return json.dumps(_snapshot_value(value), sort_keys=True, separators=(",", ":"), default=str)


def values_equal(first: Any, second: Any) -> bool:
    """Whether two asserted values are the same (canonical comparison)."""
    return _canonical_value(first) == _canonical_value(second)


def claim_assertions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """First source plus bounded reassertions of exactly the same claim value."""
    confirmations = entry.get("confirmations", [])
    return [entry, *[
        item for item in confirmations if isinstance(item, dict)
        and values_equal(item.get("value"), entry.get("value"))
    ]] if isinstance(confirmations, list) else [entry]


def retain_claim_confirmation(
    original: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Preserve the sticky first source while retaining equal edge assertions.

    The original value/source never changes. A measured reobservation is a
    separate witness; unrelated edge attributes are separate claim entries.
    """
    if not values_equal(original.get("value"), current.get("value")):
        return original
    identity_fields = ("run_id", "evidence_id", "source", "confidence")
    identity = tuple(current.get(name) for name in identity_fields)
    if any(tuple(item.get(name) for name in identity_fields) == identity
           for item in claim_assertions(original)):
        return original
    confirmations = claim_assertions(original)[1:]
    return {
        **original,
        "confirmations": [*confirmations, current][-MAX_PROVENANCE_ENTRIES:],
    }


def _digest_value(value: Any) -> str:
    """Short digest identifying one list element across merges."""
    return hashlib.sha256(_canonical_value(value).encode()).hexdigest()[:16]


def digest_value(value: Any) -> str:
    """Short digest identifying one asserted value (projection side)."""
    return _digest_value(value)


def _claim_meta(provenance: Provenance | None, step: int) -> dict[str, Any] | None:
    """Write source without any value snapshot."""
    if provenance is None:
        return None
    confidence = (
        provenance.confidence.value
        if isinstance(provenance.confidence, Confidence)
        else str(provenance.confidence)
    )
    return {
        "source": provenance.source,
        "evidence_id": provenance.evidence_id,
        "evidence": provenance.evidence_id,
        "run_id": provenance.run_id,
        "run": provenance.run_id,
        "confidence": confidence,
        "step": step,
    }


def _element_entries(elements: Any, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-element assertions for one asserted list, deduplicated by digest."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in elements or []:
        digest = _digest_value(item)
        if digest in seen:
            continue
        seen.add(digest)
        out.append({"digest": digest, "value": _snapshot_value(item), **dict(meta)})
    return out


def _claim_entry_for_value(
    provenance: Provenance | None, step: int, value: Any
) -> dict[str, Any] | None:
    """One claim-map entry: write source plus the asserted value snapshot.

    Lists additionally carry per-element assertions, so a later union with
    an older list cannot promote elements this write never saw.
    """
    meta = _claim_meta(provenance, step)
    if meta is None:
        return None
    entry = dict(meta)
    entry["value"] = _snapshot_value(value)
    if isinstance(value, list):
        entry["elements"] = _element_entries(value, meta)
    return entry


def _claim_entries_for_write(
    *,
    attrs: dict[str, Any] | None,
    facts: dict[str, Any] | None,
    provenance: Provenance | None,
    step: int,
) -> dict[str, dict[str, Any]]:
    """Fresh claim entries for every key one write asserts, each with the

    asserted value snapshot the projection judges against.
    """
    if provenance is None:
        return {}
    claims: dict[str, dict[str, Any]] = {}
    for key, value in (attrs or {}).items():
        entry = _claim_entry_for_value(provenance, step, value)
        if entry is not None:
            claims[claim_key_for_attr(str(key))] = entry
    for key, value in (facts or {}).items():
        entry = _claim_entry_for_value(provenance, step, value)
        if entry is not None:
            claims[claim_key_for_fact(str(key))] = entry
    return claims


def claim_entries_for_fact_keys(
    props: dict[str, Any] | Any, provenance: Provenance | None, step: int
) -> dict[str, dict[str, Any]]:
    """Claim entries for flat property keys (edges store relation content

    without an attrs/facts split, so each key is recorded as ``facts:<key>``).
    A mapping snapshots each asserted value; a bare key list records the
    source only.
    """
    if isinstance(props, dict):
        return _claim_entries_for_write(attrs={}, facts=props, provenance=provenance, step=step)
    meta = _claim_meta(provenance, step)
    if meta is None:
        return {}
    return {claim_key_for_fact(str(key)): dict(meta) for key in props or []}


def attrs_observed_at(properties: dict[str, Any]) -> dict[str, int]:
    """Step each predicate was last asserted at."""
    value = properties.get(ATTRS_SEEN_KEY)
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, int)}


def attr_age(properties: dict[str, Any], key: str, current_step: int) -> int:
    """Turns since ``key`` was last asserted (node age if never recorded)."""
    seen = attrs_observed_at(properties).get(key)
    return current_step - (last_seen_step(properties) if seen is None else seen)


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


#: Retraction entries that record which observation lineage they withdrew
#: carry this version. Older entries only have step/key/reason.
RETRACTION_LINEAGE_VERSION = 1


def version_of(properties: dict[str, Any]) -> int:
    # Row version of one node properties mapping (0 for legacy rows).
    value = properties.get(OBSERVATION_VERSION_KEY, 0)
    return value if isinstance(value, int) else 0


def _retractions_by_key(retractions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    # All retraction entries per attr in row-update order; unknown shapes ignored.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in retractions:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", ""))
        step = entry.get("step", 0)
        if not isinstance(step, int):
            continue
        grouped.setdefault(key, []).append(entry)
    return grouped


def _claim_identity(entry: dict[str, Any]) -> tuple[Any, Any, Any] | None:
    # Which observation wrote one claim entry: (run, evidence, source).
    # None for unsourced writes, which keep the legacy numeric comparison.
    if not isinstance(entry, dict):
        return None
    run_id = entry.get("run_id")
    evidence_id = entry.get("evidence_id")
    source = entry.get("source")
    if run_id is None and evidence_id is None and source is None:
        return None
    return (run_id, evidence_id, source)


def _withdrawn_identities(entry: dict[str, Any]) -> set[tuple[Any, Any, Any]] | None:
    # Observation identities one retraction withdrew; None for legacy entries.
    withdrawn = entry.get("withdrawn")
    if not isinstance(withdrawn, list):
        return None
    identities: set[tuple[Any, Any, Any]] = set()
    for item in withdrawn:
        identity = _claim_identity(item) if isinstance(item, dict) else None
        if identity is not None:
            identities.add(identity)
    return identities


def _claim_asserted_version(claim: dict[str, Any] | None) -> int | None:
    if not isinstance(claim, dict):
        return None
    value = claim.get(ASSERTED_VERSION_KEY)
    return value if isinstance(value, int) else None


def _entry_blocks(
    entry: dict[str, Any],
    incoming_claim: dict[str, Any] | None,
    incoming_step: int,
    incoming_version: int | None,
) -> bool:
    # One entry bars the write when the incoming state does not postdate it.
    # Versioned snapshots compare row or claim versions against the version
    # the retraction superseded; live writes compare observation lineage;
    # anything else keeps the numeric step comparison.
    supersedes = entry.get("supersedes_version")
    asserted = _claim_asserted_version(incoming_claim)
    if asserted is not None and isinstance(supersedes, int):
        return asserted <= supersedes
    if asserted is None and incoming_version is not None and isinstance(supersedes, int):
        return incoming_version <= supersedes
    if asserted is not None or incoming_version is not None:
        step = entry.get("step", 0)
        return incoming_step <= (step if isinstance(step, int) else 0)
    withdrawn = _withdrawn_identities(entry)
    identity = _claim_identity(incoming_claim) if incoming_claim is not None else None
    if withdrawn is None or identity is None:
        step = entry.get("step", 0)
        return incoming_step <= (step if isinstance(step, int) else 0)
    return identity in withdrawn


def _restores_predicate(
    entries: list[dict[str, Any]],
    incoming_claim: dict[str, Any] | None,
    incoming_step: int,
    incoming_version: int | None,
) -> bool:
    # A retracted predicate comes back only for state that postdates every
    # recorded withdrawal: a newer snapshot version, or a live observation
    # with lineage never withdrawn here.
    return not any(
        _entry_blocks(entry, incoming_claim, incoming_step, incoming_version)
        for entry in entries
    )


def _entry_newer_than_claim(
    entry: dict[str, Any], current_claim: dict[str, Any] | None, observed_step: int
) -> bool:
    # A merged retraction withdraws a held predicate only when the
    # retraction is newer than the current establishment of the predicate.
    supersedes = entry.get("supersedes_version")
    asserted = _claim_asserted_version(current_claim)
    if isinstance(supersedes, int) and asserted is not None:
        return supersedes > asserted
    step = entry.get("step", 0)
    return observed_step <= (step if isinstance(step, int) else 0)


def _withdraws_current(
    entries: list[dict[str, Any]],
    current_claim: dict[str, Any] | None,
    observed_step: int,
) -> bool:
    return any(
        _entry_newer_than_claim(entry, current_claim, observed_step) for entry in entries
    )


def _merge_attrs(
    existing: dict[str, AttrValue],
    incoming: dict[str, AttrValue],
    *,
    retractions: dict[str, list[dict[str, Any]]] | None = None,
    incoming_claims: dict[str, dict[str, Any]] | None = None,
    existing_claims: dict[str, dict[str, Any]] | None = None,
    incoming_step: int = 0,
    incoming_version: int | None = None,
    seen_at: dict[str, int] | None = None,
) -> dict[str, AttrValue]:
    merged = dict(existing)
    blocked = retractions or {}
    observed = seen_at or {}
    claims = incoming_claims or {}
    current = existing_claims or {}

    # A retraction arriving from the other side must actually withdraw the
    # predicate, not merely refuse to re-add it - but only when the
    # retraction is newer than the current establishment of the predicate.
    # A stale tombstone reimport must not remove a later re-observation.
    for key, entries in blocked.items():
        if key not in merged or key in incoming:
            continue
        if _withdraws_current(
            entries, current.get(claim_key_for_attr(key)), observed.get(key, 0)
        ):
            merged.pop(key)
    for key, value in incoming.items():
        # A withdrawn predicate must not come back from a stale snapshot
        # import, but a genuinely new observation restores it - that is what
        # re-observing is for. Row versions tell the two apart; see
        # _restores_predicate.
        pending = blocked.get(key)
        if pending is not None and not _restores_predicate(
            pending, claims.get(claim_key_for_attr(key)), incoming_step, incoming_version
        ):
            continue
        current_value = merged.get(key)
        # Sticky True: something observed once stays observed until it is
        # explicitly retracted with a reason.
        if isinstance(current_value, bool) and isinstance(value, bool):
            merged[key] = current_value or value
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
    if (incoming.get("created_evidence_id")
            and incoming.get("created_by_action_id") != existing.get("created_by_action_id")):
        from datetime import datetime
        try:
            created = datetime.fromisoformat(str(incoming["created_at"]).replace("Z", "+00:00"))
            previous = existing.get("created_at")
            if previous is None or created > datetime.fromisoformat(
                str(previous).replace("Z", "+00:00"),
            ):
                return incoming
        except (ValueError, KeyError, TypeError):
            pass  # A different label alone cannot reset a cleaned resource.
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
            limit = MAX_FACT_LIST_ITEMS if key in CAPPED_LIST_FACTS else None
            joined = _union(current, value, limit=limit)
            if len(joined) > WARN_FACT_LIST_ITEMS:
                logger.warning(
                    "fact list %r has %d items — kept in full, but this is far past "
                    "anything a run should produce",
                    key,
                    len(joined),
                )
            merged[key] = joined
        else:
            merged[key] = value
    return merged


_MISSING: Any = object()


def _merge_elements(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    merged_values: list[Any],
) -> list[dict[str, Any]]:
    """Union of per-element assertions following the merged list order.

    An element already recorded keeps its entry (sticky); only genuinely
    new elements take the incoming source. Elements from an unsourced side
    get no entry, so the projection drops them instead of promoting them.
    """
    by_digest: dict[str, dict[str, Any]] = {}
    for item in existing:
        if isinstance(item, dict) and item.get("digest"):
            by_digest.setdefault(str(item["digest"]), item)
    for item in incoming:
        if isinstance(item, dict) and item.get("digest"):
            by_digest.setdefault(str(item["digest"]), item)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in merged_values or []:
        digest = _digest_value(value)
        if digest in seen:
            continue
        seen.add(digest)
        found = by_digest.get(digest)
        if found is not None:
            out.append(dict(found))
    return out


def merge_claim_provenance(
    existing: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
    *,
    merged_attrs: dict[str, AttrValue],
    merged_facts: dict[str, Any],
    existing_attrs: dict[str, AttrValue],
    existing_facts: dict[str, Any],
    incoming_attrs: dict[str, AttrValue],
    incoming_facts: dict[str, Any],
    retractions: dict[str, list[dict[str, Any]]] | None = None,
    incoming_step: int = 0,
    incoming_version: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-key claim source merge matching the attr/fact merge rules.

    A claim entry only moves when its value actually moved. An unchanged
    value keeps its entry (sticky) even when the latest write is measured;
    a sticky-``True`` predicate that ignored an incoming ``False``, a fact
    whose incoming value was ``None``, or a retraction-blocked older write
    likewise leaves the existing entry in place. List values merge
    per-element: recorded elements keep their source, only new elements
    take the incoming one. Entries for retracted predicates are dropped.
    Writes without a claim entry (no provenance) never erase a recorded one.
    """
    blocked = retractions or {}
    merged: dict[str, dict[str, Any]] = {}
    for claim_key, entry in existing.items():
        if claim_key.startswith(CLAIM_ATTR_PREFIX):
            if str(claim_key[len(CLAIM_ATTR_PREFIX) :]) in merged_attrs:
                merged[claim_key] = dict(entry)
        elif claim_key.startswith(CLAIM_FACT_PREFIX):
            if str(claim_key[len(CLAIM_FACT_PREFIX) :]) in merged_facts:
                merged[claim_key] = dict(entry)
    for claim_key, entry in incoming.items():
        if not isinstance(entry, dict):
            continue
        if claim_key.startswith(CLAIM_ATTR_PREFIX):
            key = str(claim_key[len(CLAIM_ATTR_PREFIX) :])
            if key not in merged_attrs:
                continue
            entries = blocked.get(key)
            if entries is not None and not _restores_predicate(
                entries, incoming.get(claim_key), incoming_step, incoming_version
            ):
                continue
            if key not in incoming_attrs:
                continue
            arrived = incoming_attrs.get(key, _MISSING)
            current = existing_attrs.get(key, _MISSING)
            if current is not _MISSING and values_equal(arrived, current):
                continue  # unchanged value keeps its source (sticky)
            if not values_equal(merged_attrs[key], arrived):
                continue  # not applied (sticky True); source stays
            merged[claim_key] = dict(entry)
        elif claim_key.startswith(CLAIM_FACT_PREFIX):
            key = str(claim_key[len(CLAIM_FACT_PREFIX) :])
            if key not in merged_facts:
                continue
            if key not in incoming_facts:
                continue
            arrived = incoming_facts.get(key, _MISSING)
            if arrived is None:
                continue  # None never erases; source stays
            current = existing_facts.get(key, _MISSING)
            if current is not _MISSING and values_equal(arrived, current):
                continue  # unchanged value keeps its source (sticky)
            if isinstance(arrived, list) and isinstance(current, list):
                merged_list = merged_facts[key]
                if not isinstance(merged_list, list):
                    continue
                old_elements = existing.get(claim_key, {}).get("elements", [])
                new_elements = entry.get("elements", [])
                if not isinstance(old_elements, list):
                    old_elements = []
                if not isinstance(new_elements, list):
                    new_elements = []
                merged[claim_key] = {
                    **dict(entry),
                    "value": _snapshot_value(merged_list),
                    "elements": _merge_elements(old_elements, new_elements, merged_list),
                }
                continue
            if not values_equal(merged_facts[key], arrived):
                continue  # not applied; source stays
            merged[claim_key] = dict(entry)
    return merged


def merge_properties(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold a fresh observation into what the graph already holds.

    Never destructive: sticky ``True`` attrs, list union, incoming ``None``
    ignored, ``last_seen_step`` moves forward only.
    """
    merged: dict[str, Any] = {
        key: value for key, value in existing.items() if key not in RESERVED_KEYS
    }
    for key, value in incoming.items():
        if key not in RESERVED_KEYS:
            merged[key] = value

    retractions = _union(retractions_of(existing), retractions_of(incoming))
    merged[RETRACTIONS_KEY] = retractions

    existing_seen = attrs_observed_at(existing) or dict.fromkeys(
        attrs_of(existing), last_seen_step(existing)
    )
    incoming_seen = attrs_observed_at(incoming) or dict.fromkeys(
        attrs_of(incoming), last_seen_step(incoming)
    )
    existing_attrs = attrs_of(existing)
    incoming_attrs = attrs_of(incoming)
    retraction_map = _retractions_by_key(retractions)
    incoming_claims = claim_provenance_of(incoming)
    raw_version = incoming.get(OBSERVATION_VERSION_KEY)
    incoming_version = raw_version if isinstance(raw_version, int) else None
    incoming_step = last_seen_step(incoming)
    merged[ATTRS_KEY] = _merge_attrs(
        existing_attrs,
        incoming_attrs,
        retractions=retraction_map,
        incoming_claims=incoming_claims,
        existing_claims=claim_provenance_of(existing),
        incoming_step=incoming_step,
        incoming_version=incoming_version,
        seen_at=existing_seen,
    )
    merged[ATTRS_SEEN_KEY] = {
        key: max(existing_seen.get(key, 0), incoming_seen.get(key, 0)) for key in merged[ATTRS_KEY]
    }
    existing_facts = facts_of(existing)
    incoming_facts = facts_of(incoming)
    merged[FACTS_KEY] = _merge_facts(existing_facts, incoming_facts)
    merged[PROVENANCE_KEY] = _cap_provenance(
        _union(provenance_of(existing), provenance_of(incoming))
    )
    merged[CLAIM_PROVENANCE_KEY] = merge_claim_provenance(
        claim_provenance_of(existing),
        claim_provenance_of(incoming),
        merged_attrs=merged[ATTRS_KEY],
        merged_facts=merged[FACTS_KEY],
        existing_attrs=existing_attrs,
        existing_facts=existing_facts,
        incoming_attrs=incoming_attrs,
        incoming_facts=incoming_facts,
        retractions=retraction_map,
        incoming_step=incoming_step,
        incoming_version=incoming_version,
    )

    cleanup = _merge_cleanup(cleanup_of(existing), cleanup_of(incoming))
    if cleanup is not None:
        merged[CLEANUP_KEY] = cleanup

    if OBSERVATION_VERSION_KEY in existing or OBSERVATION_VERSION_KEY in incoming:
        merged[OBSERVATION_VERSION_KEY] = max(version_of(existing), version_of(incoming))
    merged[FIRST_SEEN_KEY] = min(first_seen_step(existing), first_seen_step(incoming))
    merged[LAST_SEEN_KEY] = max(last_seen_step(existing), last_seen_step(incoming))
    return merged


def _cap_provenance(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first entry and the most recent window of the rest."""
    if len(entries) <= MAX_PROVENANCE_ENTRIES:
        return entries
    return [entries[0], *entries[-(MAX_PROVENANCE_ENTRIES - 1) :]]


def apply_retraction(
    properties: dict[str, Any],
    key: str,
    *,
    step: int,
    reason: str,
    provenance: Provenance | None = None,
) -> tuple[dict[str, Any], bool]:
    """Withdraw one predicate, recording why. ``reason`` is required.

    The withdrawn claim lineage is recorded on the entry so a later merge
    can tell a genuinely new observation from a stale snapshot replay.
    Returns the new properties and whether anything was withdrawn.
    """
    if not reason:
        raise ValueError("a retraction needs a reason — otherwise the state change is untraceable")

    attrs = attrs_of(properties)
    if key not in attrs:
        return properties, False

    attrs.pop(key)
    claims = claim_provenance_of(properties)
    withdrawn_entry = claims.pop(claim_key_for_attr(key), None)
    withdrawn = claim_assertions(withdrawn_entry) if withdrawn_entry is not None else []
    new_version = version_of(properties) + 1
    entry: dict[str, Any] = {
        "step": step,
        "key": key,
        "reason": reason,
        "lineage_version": RETRACTION_LINEAGE_VERSION,
        "supersedes_version": new_version,
        "withdrawn": [dict(item) for item in withdrawn if isinstance(item, dict)],
    }
    if provenance is not None:
        entry["retracted_by"] = _claim_meta(provenance, step)
    retractions = retractions_of(properties)
    retractions.append(entry)

    seen = attrs_observed_at(properties)
    seen.pop(key, None)

    updated = dict(properties)
    updated[OBSERVATION_VERSION_KEY] = new_version
    updated[ATTRS_KEY] = attrs
    updated[RETRACTIONS_KEY] = retractions
    updated[ATTRS_SEEN_KEY] = seen
    updated[CLAIM_PROVENANCE_KEY] = claims
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
