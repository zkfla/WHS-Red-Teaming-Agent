"""Window egress admissions: metering plus destination-attempt linkage.

Renamed semantics (root-amended 004A contract): an ACCEPTED egress
admission is metering and a destination *attempt* record. It is NOT
contact, NOT a reached peer, NOT an application response, NOT
remote content, and NOT session provenance. The word "contact" applies
only when actual later-hook or response evidence proves delivery.

Each admission entry carries the OBSERVED hook (where the boundary saw
it, e.g. the flow-init queue or the post-accept counter) and whatever
quantity was observed there. An accepted admission can still hit
quota, drop, or expiry before outbound delivery, so acceptance never
upgrades an attempt into a measured remote fact on its own.

These helpers are pure. Evidence display uses validation for exact-holder
companion linkage, not admission or remote proof. ``_write_fact`` explicitly
refuses shell envelopes as remote measured facts even with admissions; the
remote L3 hook/response subject contract stays on HOLD until concrete
observable evidence exists. Validation here only decides whether a
companion is well-formed metering, never whether a fact is true.
"""

from __future__ import annotations

from typing import Any

#: Companion fields 004A needs for association. The storage envelope
#: (record kind, source type/id) belongs to the emitting provider and
#: the journal; only these linkage fields are validated here.
_REQUIRED_TOP_FIELDS = ("window_id", "run_id", "instance_id", "epoch")

#: Holder fields binding one companion to exactly one execution. The
#: holder is stamped at window open from the trusted invocation; a
#: companion without it is metering-only and can never become subject
#: linkage (time-overlap joins are forbidden).
_REQUIRED_HOLDER_FIELDS = ("action_id", "invocation_id")


def _nonblank_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_port(value: Any) -> bool:
    return type(value) is int and 0 < value <= 65535


def _validate_scope(scope: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(scope, list) or not scope:
        return ["scope must be a non-empty list of allowed flows"]
    for index, flow in enumerate(scope):
        where = f"scope[{index}]"
        if not isinstance(flow, dict):
            errors.append(where + " must be an object")
            continue
        if not _nonblank_str(flow.get("ip")):
            errors.append(where + ".ip must be a non-empty string")
        if not _is_port(flow.get("port")):
            errors.append(where + ".port must be an integer 1..65535")
        if flow.get("proto") not in ("tcp", "udp"):
            errors.append(where + ".proto must be tcp or udp")
    return errors


def _validate_admission(entry: Any, index: int, companion: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    where = f"admissions[{index}]"
    if not isinstance(entry, dict):
        return [where + " must be an object"]
    for field in (
        "window_id",
        "run_id",
        "instance_id",
        "epoch",
        "daemon_session_id",
        "seq",
        "flow_key",
        "dst_ip",
        "dst_port",
        "verdict",
    ):
        if entry.get(field) is None or (isinstance(entry.get(field), str) and not entry.get(field)):
            errors.append(where + "." + field + " must be present")
    if not _nonblank_str(entry.get("dst_ip")):
        errors.append(where + ".dst_ip must be a non-empty string")
    if not _is_port(entry.get("dst_port")):
        errors.append(where + ".dst_port must be an integer 1..65535")
    if not _nonblank_str(entry.get("verdict")):
        errors.append(where + ".verdict must be a non-empty string")
    if not _nonblank_str(entry.get("observed_hook")):
        errors.append(where + ".observed_hook must name the hook that saw it")
    for key in ("metered_packets", "metered_bytes"):
        if key in entry and not (type(entry[key]) is int and entry[key] >= 0):
            errors.append(where + "." + key + " must be a non-negative integer when present")
    for field in ("window_id", "run_id", "instance_id", "epoch"):
        if entry.get(field) != companion.get(field):
            errors.append(where + "." + field + " must equal the companion " + field)
    return errors


def validate_admission_companion(companion: Any) -> list[str]:
    """Well-formedness errors for a window egress-admission companion.

    Empty means well-formed metering. Well-formed never means true,
    contacted, or subject-ready: use is restricted to metering plus
    destination-attempt linkage, and association additionally requires
    the holder rule below. No verdict value constitutes contact.
    """
    if not isinstance(companion, dict):
        return ["companion must be an object"]
    errors: list[str] = []
    for field in _REQUIRED_TOP_FIELDS:
        value = companion.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(field + " must be present")
    holder = companion.get("holder")
    if not isinstance(holder, dict):
        errors.append("holder binding required: companion without an exact holder is metering-only")
    else:
        for field in _REQUIRED_HOLDER_FIELDS:
            if not _nonblank_str(holder.get(field)):
                errors.append(
                    "holder binding required: holder." + field + " must be a non-empty string"
                )
    errors.extend(_validate_scope(companion.get("scope")))
    admissions = companion.get("admissions")
    if not isinstance(admissions, list):
        errors.append("admissions must be a list (possibly empty)")
    else:
        for index, entry in enumerate(admissions):
            errors.extend(_validate_admission(entry, index, companion))
    return errors


def destination_attempts(companion: dict[str, Any]) -> list[dict[str, Any]]:
    """Destination attempts projected from a validated companion.

    Each item names dst_ip/dst_port/verdict/observed_hook only. The key
    name says attempt on purpose: callers must never read these as
    contacted peers, application responses, or session provenance.
    Assumes validate_admission_companion(companion) == []; malformed
    entries are skipped, never repaired.
    """
    attempts: list[dict[str, Any]] = []
    admissions = companion.get("admissions")
    if not isinstance(admissions, list):
        return attempts
    for entry in admissions:
        if not isinstance(entry, dict):
            continue
        if not _nonblank_str(entry.get("dst_ip")) or not _is_port(entry.get("dst_port")):
            continue
        if not _nonblank_str(entry.get("verdict")) or not _nonblank_str(
            entry.get("observed_hook")
        ):
            continue
        attempts.append(
            {
                "dst_ip": entry["dst_ip"],
                "dst_port": entry["dst_port"],
                "verdict": entry["verdict"],
                "observed_hook": entry["observed_hook"],
            }
        )
    return attempts


def detect_companion_conflict(first: Any, second: Any) -> str | None:
    """Conflict token for two companions claiming the same window.

    Same window_id with differing canonical bytes is a conflict: stores
    must reject, never silently overwrite (immutable-companion rule).
    Different windows, or byte-identical companions, are not conflicts.
    Canonical form is sorted-keys JSON; non-object inputs are described,
    never raised on.
    """
    import json as _json

    if not isinstance(first, dict) or not isinstance(second, dict):
        return "companion conflict inputs must be objects"
    if first.get("window_id") != second.get("window_id"):
        return None
    if not _nonblank_str(first.get("window_id")):
        return "companions without a window_id cannot be compared"
    try:
        left = _json.dumps(first, sort_keys=True, separators=(",", ":"), default=str)
        right = _json.dumps(second, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "companions are not comparable"
    if left != right:
        return f'conflicting companions for window {first.get("window_id")}'
    return None
