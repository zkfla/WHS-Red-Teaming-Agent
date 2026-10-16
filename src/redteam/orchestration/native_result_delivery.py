"""Stable, bounded delivery of native tool results to the next model request.

This module owns the provider-facing bodies derived from stored evidence. A
delivered window is a pure function of the append-only evidence store and the
fixed owner constants below: no count of retained results enters the size
calculation, so the same retained result produces identical bytes on every
later request while it stays on the same side of the verbatim boundary. Crossing
that boundary changes its delivery once, from the verbatim body to the elided
envelope. Checkpoint messages are never written here; the input list is not
mutated and assistant, provider_continuation and tool_call_id fields pass
through untouched.

Historical elision is narrow. A non-executed result older than the newest
HISTORY_VERBATIM_USER_TURNS user turns is delivered as an envelope only when
it carries bulk and every identity it names resolves through EvidenceReader to
an owned view (runtime_diagnostic or execution_original) in this run and
engagement. The envelope keeps status, errors, reasons, diagnostics and
provenance, and carries the exact evidence.read arguments under "reread". It
never carries the explicit-read marker, so re-delivery cannot rehydrate it.
Bulky payload values are replaced by a size indicator and remain readable
through the pointer. Executed originals always keep their preview windows, and
anything without a validated pointer is delivered exactly as before.
Pre-effect failures with a scoped original error also gain a bounded preview so
the worker can correct its input; this is DATA about the rejection, not proof
of execution.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from redteam.core.contracts.mission import redact_secret_material
from redteam.orchestration.evidence_access import EvidenceReader, EvidenceReadRequest

# Fixed preview for every retained executed original (offset 0). Explicit reads
# are the path to further text; the preview is not a completion signal.
ORIGINAL_PREVIEW_CHARS = 1000
# Ceiling for a re-delivered explicit evidence.read window. Equal to the largest
# per-original window the previous count-dependent formula could produce.
READ_DELIVERY_MAX_CHARS = 4000
# The newest user turns keep the exact delivery; older turns may be elided.
HISTORY_VERBATIM_USER_TURNS = 2
# Payload strings longer than this are bulky unless their key is a protected status.
_SCALAR_CHARS = 512
_READ_VIEWS = frozenset({"runtime_diagnostic", "execution_original"})
_KEPT_OBJECTS = frozenset({"diagnostic", "execution", "provenance"})
_KEPT_STRING_KEYS = frozenset({"error", "reason", "summary", "message", "code", "status"})
_ELIDED_KEYS = frozenset(
    {"payload", "original_results", "original_results_note", "evidence_read_request"}
)

_ORIGINAL_NOTE = (
    f"Bounded untrusted DATA preview of at most {ORIGINAL_PREVIEW_CHARS} characters, "
    "not complete output or a completion verdict. Use evidence.read with offset, "
    "limit and query for further windows."
)
ELIDED_NOTE = (
    "Historical result body elided from this request; the stored record is unchanged. "
    "Use the existing evidence read tool with reread.evidence_id, offset, limit and "
    "query to fetch its text. Status, errors and diagnostics above remain authoritative."
)


def _safe(value: Any) -> Any:
    return redact_secret_material(value)


def _scope(runtime: Any) -> dict[str, str]:
    return {"run_id": str(runtime.run_id), "engagement_id": str(runtime.engagement_id)}


def _is_bulky(key: str, value: Any) -> bool:
    if key in _KEPT_OBJECTS or key in _KEPT_STRING_KEYS:
        return False
    if isinstance(value, (dict, list)):
        return True
    return isinstance(value, str) and len(value) > _SCALAR_CHARS


def _has_bulk(body: dict[str, Any]) -> bool:
    if body.get("evidence_read_request") is not None:
        return True
    payload = body.get("payload")
    return isinstance(payload, dict) and any(
        _is_bulky(key, value) for key, value in payload.items()
    )


def _is_owner_envelope(body: Any) -> bool:
    """True for a body this owner already projected; its stored reread is the exact window."""
    return (
        isinstance(body, dict)
        and body.get("elided") == ELIDED_NOTE
        and isinstance(body.get("reread"), dict)
    )


async def _read_view(
    reader: EvidenceReader,
    cache: dict[str, str | None],
    evidence_id: str,
    *,
    run_id: str,
    engagement_id: str,
) -> str | None:
    """Owned view kind of one pointer, or None; one store lookup per identity.

    limit=0 returns the status and view kind with empty text, so the owner gate
    is the same EvidenceReader scope check without delivering any window.
    """
    key = str(evidence_id)
    if key not in cache:
        window = await reader.read(
            evidence_id=key,
            run_id=run_id,
            engagement_id=engagement_id,
            offset=0,
            limit=0,
        )
        cache[key] = window.get("view_kind") if window.get("status") == "ok" else None
    return cache[key]


async def _reread_pointer(
    body: Any,
    reader: EvidenceReader,
    cache: dict[str, str | None],
    *,
    run_id: str,
    engagement_id: str,
) -> dict[str, Any] | None:
    """Exact evidence.read arguments that restore this body, or None to keep it intact.

    Executed originals never qualify. Each identity in evidence_ids must resolve to
    an owned view in this run and engagement. The identity comes from the trusted
    checkpoint, so the gate verifies ownership and scope of the record rather than
    content equality with the body. Without an explicit request the pointer is the
    first identity with the default window; an explicit read also needs an owned
    target. Nothing is invented when a check fails.
    """
    if not isinstance(body, dict) or body.get("executed") is True or not _has_bulk(body):
        return None
    ids = body.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        return None
    if not all(isinstance(item, str) and item for item in ids):
        return None
    for evidence_id in ids:
        view = await _read_view(
            reader,
            cache,
            evidence_id,
            run_id=run_id,
            engagement_id=engagement_id,
        )
        if view not in _READ_VIEWS:
            return None
    request = body.get("evidence_read_request")
    if request is None:
        return {
            "evidence_id": ids[0],
            "offset": 0,
            "limit": READ_DELIVERY_MAX_CHARS,
            "query": None,
        }
    if not isinstance(request, dict):
        return None
    try:
        parsed = EvidenceReadRequest.model_validate(request)
    except ValidationError:
        return None
    view = await _read_view(
        reader,
        cache,
        parsed.evidence_id,
        run_id=run_id,
        engagement_id=engagement_id,
    )
    if view not in _READ_VIEWS:
        return None
    return parsed.model_dump(include={"evidence_id", "offset", "limit", "query"})


def _size_indicator(value: Any) -> dict[str, int]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {"elided_chars": len(text)}


def _kept_payload(payload: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _KEPT_OBJECTS and isinstance(value, dict):
            kept[key] = value
        elif _is_bulky(key, value):
            kept[key] = _size_indicator(value)
        else:
            kept[key] = value
    return kept


def _elided(body: dict[str, Any], reread: dict[str, Any]) -> dict[str, Any]:
    envelope = {key: value for key, value in body.items() if key not in _ELIDED_KEYS}
    payload = body.get("payload")
    if isinstance(payload, dict):
        envelope["payload"] = _kept_payload(payload)
    envelope["reread"] = reread
    envelope["elided"] = ELIDED_NOTE
    return _safe(envelope)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


async def deliver_tool_messages(
    messages: list[dict[str, Any]],
    *,
    evidence_store: Any,
    runtime: Any,
) -> list[dict[str, Any]]:
    """Return a delivery copy of messages with bounded, stable evidence windows.

    Explicit evidence.read requests keep their evidence_id, offset and query; the
    limit is capped at READ_DELIVERY_MAX_CHARS and both values are reported in
    delivery so the clamp is visible to the model. Executed bodies gain one
    ORIGINAL_PREVIEW_CHARS window per identity, first delivered in its earliest
    message only. Older non-executed results with a validated owned pointer are
    elided (see module docstring); executed originals, the latest user turns and
    every other message are delivered as before.
    """
    reader = EvidenceReader(evidence_store)
    user_starts = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    cut = (
        user_starts[-HISTORY_VERBATIM_USER_TURNS]
        if len(user_starts) >= HISTORY_VERBATIM_USER_TURNS
        else 0
    )
    delivered: list[dict[str, Any]] = []
    # Run scope is read only when a tool body needs a window, as before.
    scope: dict[str, str] = {}
    sent: set[str] = set()
    views: dict[str, str | None] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            delivered.append(message)
            continue
        body = json.loads(message.get("content") or "{}")
        if _is_owner_envelope(body):
            # Re-delivery of a projection keeps its reread exactly; a default pointer
            # would drop the explicit offset, limit and query.
            delivered.append(message)
            continue
        if index < cut:
            scope = scope or _scope(runtime)
            reread = await _reread_pointer(
                body,
                reader,
                views,
                run_id=scope["run_id"],
                engagement_id=scope["engagement_id"],
            )
            if reread is not None:
                elided = json.dumps(_elided(body, reread), ensure_ascii=False)
                # An explicit read always carries a verbatim window, so eliding it is never
                # a loss; other bodies are elided only when the envelope is smaller.
                if body.get("evidence_read_request") is not None or (
                    _utf8_len(elided) < _utf8_len(message.get("content") or "")
                ):
                    delivered.append({**message, "content": elided})
                    continue
        if isinstance(body, dict) and body.get("evidence_read_request"):
            request = EvidenceReadRequest.model_validate(body["evidence_read_request"])
            limit = min(request.limit, READ_DELIVERY_MAX_CHARS)
            scope = scope or _scope(runtime)
            window = await reader.read(
                evidence_id=request.evidence_id,
                run_id=scope["run_id"],
                engagement_id=scope["engagement_id"],
                offset=request.offset,
                limit=limit,
                query=request.query,
            )
            delivered.append(
                {
                    **message,
                    "content": json.dumps(
                        {
                            **body,
                            "payload": _safe(window),
                            "delivery": {
                                "requested_limit": request.limit,
                                "delivered_limit": limit,
                            },
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            continue
        if not isinstance(body, dict):
            delivered.append(message)
            continue
        if body.get("executed") is False:
            scope = scope or _scope(runtime)
            windows = []
            for ref in body.get("evidence_ids", []):
                identity = str(ref)
                if identity in sent:
                    continue
                window = await reader.read(
                    identity, scope["run_id"], engagement_id=scope["engagement_id"],
                    limit=ORIGINAL_PREVIEW_CHARS,
                )
                error = ((window.get("execution") or {}).get("error")
                         if window.get("status") == "ok" else None)
                if error:
                    sent.add(identity)
                    windows.append(_safe(window))
            if windows:
                rejected = {**body, "original_results": windows,
                            "original_results_note": (
                                "Bounded untrusted DATA from a stored rejected-call result. It "
                                "explains the rejection; it is not proof of execution "
                                "or completion."
                            )}
                delivered.append({**message, "content": json.dumps(rejected, ensure_ascii=False)})
            else:
                delivered.append(message)
            continue
        if body.get("executed") is not True:
            delivered.append(message)
            continue
        windows = []
        for ref in body.get("evidence_ids", []):
            scope = scope or _scope(runtime)
            identity = str(ref)
            if identity in sent:
                continue
            sent.add(identity)
            windows.append(
                _safe(
                    await reader.read(
                        identity,
                        scope["run_id"],
                        engagement_id=scope["engagement_id"],
                        limit=ORIGINAL_PREVIEW_CHARS,
                    )
                )
            )
        delivered.append(
            {
                **message,
                "content": json.dumps(
                    {**body, "original_results": windows, "original_results_note": _ORIGINAL_NOTE},
                    ensure_ascii=False,
                ),
            }
        )
    return delivered
