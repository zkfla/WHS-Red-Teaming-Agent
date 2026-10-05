"""Shared actual-use session receipt collector (B/D shared reference).

Actual receipts only, no stores and no second origin. A response without
an actual receipt has unknown session use and contributes no proof.
Request echoes and configured inventory never prove use, including in
old records. A partial receipt never borrows the unknown side's echo.
Both the analysis verifier and the completion account checks read the
same rule through this module instead of duplicating it.
"""

from __future__ import annotations

from typing import Any

_ACTUAL_TOP_KEYS = (
    "actual_session_ref",
    "first_actual_session_ref",
    "second_actual_session_ref",
)
_ACTUAL_PAIR_KEYS = ("first_actual_session_ref", "second_actual_session_ref")
_NESTED_UNITS = ("first", "second", "authenticated", "anonymous")


def _cleaned(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def collect_actual_sessions(metadata: Any, raw: Any) -> set[str]:
    """Return named actual-use receipts; absent/null receipts add no names.

    Old response payloads remain readable, but their request echoes are
    not actual-use proof. Session-to-account artifact proof is separate.
    """
    return _actual_values_from_metadata(metadata) | _actual_values_from_raw(raw)


def has_actual_receipt(metadata: Any, raw: Any) -> bool:
    """True when any request unit carries an actual receipt key.

    Null (anonymous side) counts as a present receipt, but contributes no
    named session to the collector. Absence is unknown, not anonymous.
    """
    if isinstance(metadata, dict):
        http_obs = metadata.get("http_observation")
        if isinstance(http_obs, dict) and "actual_session_ref" in http_obs:
            return True
        comparison = metadata.get("session_comparison")
        if isinstance(comparison, dict) and any(
            key in comparison for key in _ACTUAL_PAIR_KEYS
        ):
            return True
    for output in (_output_of(raw),):
        if isinstance(output, dict):
            if any(key in output for key in _ACTUAL_TOP_KEYS):
                return True
            for nested_key in _NESTED_UNITS:
                nested = output.get(nested_key)
                if isinstance(nested, dict) and "actual_session_ref" in nested:
                    return True
    return False


def actual_receipt_conflict(metadata: Any, raw: Any) -> str | None:
    """Public reason when preserved actual receipts disagree across layers.

    Metadata derivations and the raw output answer the same response; a
    disjoint actual set means the proof cannot be trusted. Echoes never
    count here: http_observation.session_ref is a request echo by
    construction, and an echo beside an actual is exclusion, not conflict.
    """
    meta_actual = _actual_values_from_metadata(metadata)
    raw_actual = _actual_values_from_raw(raw)
    if meta_actual and raw_actual and meta_actual.isdisjoint(raw_actual):
        return "actual session receipts disagree across layers"
    return None


def _output_of(raw: Any) -> Any | None:
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    return output


def _actual_values_from_metadata(metadata: Any) -> set[str]:
    values: set[str] = set()
    if not isinstance(metadata, dict):
        return values
    http_obs = metadata.get("http_observation")
    if isinstance(http_obs, dict) and "actual_session_ref" in http_obs:
        cleaned = _cleaned(http_obs.get("actual_session_ref"))
        if cleaned is not None:
            values.add(cleaned)
    comparison = metadata.get("session_comparison")
    if isinstance(comparison, dict):
        for key in _ACTUAL_PAIR_KEYS:
            if key in comparison:
                cleaned = _cleaned(comparison.get(key))
                if cleaned is not None:
                    values.add(cleaned)
    return values
def _actual_values_from_raw(raw: Any) -> set[str]:
    values: set[str] = set()
    output = _output_of(raw)
    if not isinstance(output, dict):
        return values
    for key in _ACTUAL_TOP_KEYS:
        if key in output:
            cleaned = _cleaned(output.get(key))
            if cleaned is not None:
                values.add(cleaned)
    for nested_key in _NESTED_UNITS:
        nested = output.get(nested_key)
        if isinstance(nested, dict) and "actual_session_ref" in nested:
            cleaned = _cleaned(nested.get("actual_session_ref"))
            if cleaned is not None:
                values.add(cleaned)
    return values
