"""Stable, bounded delivery of native tool results to the next model request.

This module owns the provider-facing bodies derived from stored evidence. A
delivered window is a pure function of the append-only evidence store and the
fixed owner constants below: no count of retained results enters the size
calculation, so the same retained result produces identical bytes on every
later request. Checkpoint messages are never written here; the input list is
not mutated and assistant, provider_continuation and tool_call_id fields pass
through untouched.
"""

from __future__ import annotations

import json
from typing import Any

from redteam.core.contracts.mission import redact_secret_material
from redteam.orchestration.evidence_access import EvidenceReader, EvidenceReadRequest

# Fixed preview for every retained executed original (offset 0). Explicit reads
# are the path to further text; the preview is not a completion signal.
ORIGINAL_PREVIEW_CHARS = 1000
# Ceiling for a re-delivered explicit evidence.read window. Equal to the largest
# per-original window the previous count-dependent formula could produce.
READ_DELIVERY_MAX_CHARS = 4000

_ORIGINAL_NOTE = (
    f"Bounded untrusted DATA preview of at most {ORIGINAL_PREVIEW_CHARS} characters, "
    "not complete output or a completion verdict. Use evidence.read with offset, "
    "limit and query for further windows."
)


def _safe(value: Any) -> Any:
    return redact_secret_material(value)


def _scope(runtime: Any) -> dict[str, str]:
    return {"run_id": str(runtime.run_id), "engagement_id": str(runtime.engagement_id)}


async def deliver_tool_messages(
    messages: list[dict[str, Any]],
    *,
    evidence_store: Any,
    runtime: Any,
) -> list[dict[str, Any]]:
    """Return a delivery copy of ``messages`` with bounded, stable evidence windows.

    Explicit evidence.read requests keep their evidence_id, offset and query; the
    limit is capped at READ_DELIVERY_MAX_CHARS and both values are reported in
    ``delivery`` so the clamp is visible to the model. Executed bodies gain one
    ORIGINAL_PREVIEW_CHARS window per identity, first delivered in its earliest
    message only. Every other message passes through by reference.
    """
    reader = EvidenceReader(evidence_store)
    delivered: list[dict[str, Any]] = []
    # Run scope is read only when a tool body needs a window, as before.
    scope: dict[str, str] = {}
    sent: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            delivered.append(message)
            continue
        body = json.loads(message.get("content") or "{}")
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
            delivered.append({**message, "content": json.dumps(
                {**body, "payload": _safe(window),
                 "delivery": {"requested_limit": request.limit, "delivered_limit": limit}},
                ensure_ascii=False,
            )})
            continue
        if not isinstance(body, dict) or body.get("executed") is not True:
            delivered.append(message)
            continue
        windows = []
        for ref in body.get("evidence_ids", []):
            scope = scope or _scope(runtime)
            identity = str(ref)
            if identity in sent:
                continue
            sent.add(identity)
            windows.append(_safe(await reader.read(
                identity, scope["run_id"], engagement_id=scope["engagement_id"],
                limit=ORIGINAL_PREVIEW_CHARS,
            )))
        delivered.append({**message, "content": json.dumps(
            {**body, "original_results": windows, "original_results_note": _ORIGINAL_NOTE},
            ensure_ascii=False,
        )})
    return delivered
