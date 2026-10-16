"""Shared actual-use session receipt collector (B/D shared reference).

Actual receipts only, no stores and no second origin. A response without
an actual receipt has unknown session use and contributes no proof.
Request echoes and configured inventory never prove use, including in
old records. A partial receipt never borrows the unknown side's echo.
Both the analysis verifier and the completion account checks read the
same rule through this module instead of duplicating it.

Account names follow the same receipt rule: a unit states the account it
used only beside its own actual-use receipt, so the comparison pair, the
top-level answer and each nested unit contribute their own pair and
nothing more.
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
_UNIT_ACCOUNT_PAIRS = (("principal_ref", "actual_session_ref"),)
_ACCOUNT_PAIRS = _UNIT_ACCOUNT_PAIRS + (
    ("first_principal_ref", "first_actual_session_ref"),
    ("second_principal_ref", "second_actual_session_ref"),
)
_ANONYMOUS_ACCOUNT = "anonymous"


def _cleaned(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def has_unmanaged_request_headers(arguments: Any) -> bool:
    """Whether an original request supplied custom headers with unknown auth semantics.

    The current request-header contract allowlists names but does not classify
    credential headers. A nonempty custom-header mapping therefore cannot
    support an anonymous claim, even when no registered session was used.
    """
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return bool(args.get("request_headers"))
    except Exception:
        return True


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

    Each metadata derivation must match its original request unit, including
    null vs named receipts. Malformed or contradictory receipts cannot be
    trusted. Request echoes do not participate in this comparison.
    """
    raw_units = _receipt_units(_output_of(raw))
    meta_units: dict[str, list[Any]] = {}
    if isinstance(metadata, dict):
        http = metadata.get("http_observation")
        comparison = metadata.get("session_comparison")
        for value in (http, comparison):
            for unit, receipts in _receipt_units(value).items():
                meta_units.setdefault(unit, []).extend(receipts)
    for units in (raw_units, meta_units):
        for receipts in units.values():
            if any(value is not None and _cleaned(value) is None for value in receipts):
                return "actual session receipt is malformed"
            if len({None if value is None else _cleaned(value) for value in receipts}) > 1:
                return "actual session receipts disagree within a request unit"
    for unit, receipts in meta_units.items():
        originals = raw_units.get(unit)
        if not originals:
            return "derived actual session receipt has no matching original"
        if _cleaned(receipts[0]) != _cleaned(originals[0]):
            return "actual session receipts disagree across layers"
    return None


def original_session_receipt_matches(
    metadata: Any,
    raw: Any,
    *,
    unit: str = "request",
    expected_session_ref: str | None,
) -> bool:
    """Whether one original request unit explicitly receipted its expected session.

    Only receipts on the selected original raw unit count. Metadata derivations,
    request echoes, and receipts belonging to another unit cannot supply proof.
    """
    if not isinstance(raw, dict) or raw.get("success") is not True:
        return False
    for layer in (metadata, raw.get("metadata")):
        if isinstance(layer, dict) and any(
            layer.get(key) for key in ("mock", "fixture_only")
        ):
            return False

    if expected_session_ref is None:
        expected = None
    else:
        expected = _cleaned(expected_session_ref)
        if expected is None:
            return False

    receipts = _receipt_units(_output_of(raw)).get(unit)
    if not receipts:
        return False
    normalized: list[str | None] = []
    for receipt in receipts:
        if receipt is None:
            normalized.append(None)
            continue
        cleaned = _cleaned(receipt)
        if cleaned is None:
            return False
        normalized.append(cleaned)
    if len(set(normalized)) != 1:
        return False

    return (
        actual_receipt_conflict(metadata, raw) is None
        and normalized[0] == expected
    )


def original_anonymous_receipt_matches(
    metadata: Any, raw: Any, arguments: Any
) -> bool:
    """Whether an original request unit explicitly ran anonymously.

    A nested anonymous unit is explicit on its own. A top-level request null
    is anonymous only when no named request session was selected. Null values
    on comparison legs are never promoted to an anonymous request claim.
    """
    if original_session_receipt_matches(
        metadata, raw, unit="anonymous", expected_session_ref=None
    ):
        return True
    args = arguments if isinstance(arguments, dict) else {}
    if any(
        isinstance(args.get(key), str) and args[key].strip()
        for key in ("session_ref", "first_session_ref", "second_session_ref")
    ):
        return False
    if has_unmanaged_request_headers(args):
        return False
    return original_session_receipt_matches(
        metadata, raw, unit="request", expected_session_ref=None
    )


def has_anonymous_receipt(metadata: Any, raw: Any) -> bool:
    """An explicit null on an original request unit, never an echo/declaration.

    A comparison's null side proves only anonymous use on that side. Named
    account proof remains separate. Missing, malformed and contradictory
    receipts do not prove anonymous use, including metadata-only receipts.
    """
    if not isinstance(raw, dict) or raw.get("success") is not True:
        return False
    for layer in (metadata, raw.get("metadata")):
        if isinstance(layer, dict) and any(layer.get(key) for key in ("mock", "fixture_only")):
            return False
    return (actual_receipt_conflict(metadata, raw) is None
            and any(value is None for values in _receipt_units(_output_of(raw)).values()
                    for value in values))


def _receipt_units(output: Any) -> dict[str, list[Any]]:
    if not isinstance(output, dict):
        return {}
    units: dict[str, list[Any]] = {}
    for key, unit in (("actual_session_ref", "request"),
                      ("first_actual_session_ref", "first"),
                      ("second_actual_session_ref", "second")):
        if key in output:
            units.setdefault(unit, []).append(output[key])
    for unit in _NESTED_UNITS:
        value = output.get(unit)
        if isinstance(value, dict) and "actual_session_ref" in value:
            units.setdefault(unit, []).append(value["actual_session_ref"])
    return units


def collect_actual_accounts(
    metadata: Any, raw: Any, expected_sessions: set[str] | None = None
) -> set[str]:
    """Return the accounts a response actually used, unit by unit.

    A unit names its account only beside its own actual-use receipt: the
    comparison pair, the top-level answer pair, or a nested unit's own
    actual_session_ref. Echo-only units, inventory rows and a null
    anonymous side name no account. When sessions were requested, only
    accounts receipted on those sessions contribute, unit by unit.
    """
    return {principal for session, principal in actual_account_pairs(raw)
            if principal.lower() != _ANONYMOUS_ACCOUNT
            and (not expected_sessions or session in expected_sessions)}


def actual_account_pairs(raw: Any) -> set[tuple[str, str]]:
    """Original (session, principal) pairs on the SAME request unit only.

    Derived principal declarations do not add identities to an original.
    Alias lookup and account coverage consume this same pairing rule.
    """
    values: set[tuple[str, str]] = set()
    output = _output_of(raw)
    if isinstance(output, dict):
        _add_account_pairs(values, output, _ACCOUNT_PAIRS)
        for nested_key in _NESTED_UNITS:
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                _add_account_pairs(values, nested, _UNIT_ACCOUNT_PAIRS)
    return values


def original_unit_account_pairs(raw: Any, unit: str) -> set[tuple[str, str]]:
    """Return principal/session pairs attached to one original request unit.

    Flat and nested encodings are both supported, but values from different
    units are never combined. If the same unit contains contradictory pairs,
    it contributes no account proof.
    """
    output = _output_of(raw)
    if not isinstance(output, dict):
        return set()
    if unit == "request":
        pairs = _UNIT_ACCOUNT_PAIRS
        encoded = [output]
    elif unit in {"first", "second"}:
        principal_key = f"{unit}_principal_ref"
        session_key = f"{unit}_actual_session_ref"
        pairs = ((principal_key, session_key),)
        encoded = [output]
        nested = output.get(unit)
        if isinstance(nested, dict):
            encoded.append(nested)
        # Recollect with a set so repeated flat/nested representations remain
        # one pair; multiple principal claims for a unit remain ambiguous.
        unit_values: set[tuple[str, str]] = set()
        _add_account_pairs(unit_values, output, pairs)
        if isinstance(nested, dict):
            _add_account_pairs(unit_values, nested, _UNIT_ACCOUNT_PAIRS)
        return unit_values if len(unit_values) <= 1 else set()
    elif unit in _NESTED_UNITS:
        nested = output.get(unit)
        if not isinstance(nested, dict):
            return set()
        values: set[tuple[str, str]] = set()
        _add_account_pairs(values, nested, _UNIT_ACCOUNT_PAIRS)
        return values
    else:
        return set()
    values = set()
    for item in encoded:
        _add_account_pairs(values, item, pairs)
    return values if len(values) <= 1 else set()


def _add_account_pairs(
    values: set[tuple[str, str]], unit: dict[str, Any], pairs: tuple[tuple[str, str], ...]
) -> None:
    for principal_key, session_key in pairs:
        principal = _cleaned(unit.get(principal_key))
        session = _cleaned(unit.get(session_key))
        if principal is None or session is None:
            continue
        values.add((session, principal))


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
