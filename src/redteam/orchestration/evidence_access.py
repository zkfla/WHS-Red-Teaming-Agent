"""Team-internal evidence reading for the orchestration loop.

The loop never hands a model the whole Evidence Store. EvidenceReader
serves one record of the current run at a time, with an explicit window
(offset/limit) and an optional substring filter (query), so a huge tool
output cannot blow up a prompt.

Raw ToolResult payloads live where VERIFY put them --
EvidenceRecord.metadata["tool_result"] -- there is no second store.
Three outcomes stay distinct on purpose: the record is missing
(not_found), it belongs to another run (run_mismatch), or it has no raw
payload (no_raw). Truncation is reported on the "ok" result itself
instead of being silent.

Guarantee levels: the main execution path (VERIFY) attaches the full raw
ToolResult plus proposal, path, target and account linkage, so completion
checks can treat it as grounded. Convenience records (digest-only
conversion, blocked-action notes) carry no raw payload by design and must
never count as success for completion. This module only reports which case
a record is in; it does not upgrade a convenience note into evidence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier


def __getattr__(name: str):
    # The old verifier re-export remains available without making low-level
    # payload readers import the analysis/completion layer during startup.
    if name == "StructuredLLMAnalysisVerifier":
        from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier

        return StructuredLLMAnalysisVerifier
    raise AttributeError(name)


__all__ = [
    "EvidenceReader",
    "StructuredLLMAnalysisVerifier",
    "DEFAULT_CONTEXT_CHARS",
    "ABSOLUTE_MAX_CHARS",
    "provenance_of",
    "classify_raw",
    "read_raw_record",
    "locate_raw",
    "raw_from_metadata",
]

# Default context window (characters) when the caller passes no limit.
DEFAULT_CONTEXT_CHARS = 8000
# Hard ceiling even when the caller asks for more.
ABSOLUTE_MAX_CHARS = 20000
# Raw payload keys understood by the reader, newest first.
_RAW_KEYS = ("tool_result", "raw_tool_result")


def raw_from_metadata(metadata: Any) -> Any | None:
    """First preserved raw payload under the known keys, or None.

    Key order is the contract (newest first). This never returns a windowed
    or digest view, only the full stored payload.
    """
    if not isinstance(metadata, dict):
        return None
    for key in _RAW_KEYS:
        raw = metadata.get(key)
        if raw is not None:
            return raw
    return None


def locate_raw(
    record: Any, run_id: str, engagement_id: str | None = None
) -> tuple[str, Any | None]:
    """Common gate: same run, same engagement, raw present.

    Returns (status, raw) with status one of "missing" (no record),
    "other_run" (another run, or another engagement when one is required),
    "no_raw" (a convenience or digest record by design), or "ok" with the
    full stored payload. Engagement mismatch folds into "other_run",
    matching the evidence-index diagnostics buckets.
    """
    if record is None:
        return "missing", None
    if str(getattr(record, "run_id", "")) != str(run_id):
        return "other_run", None
    if engagement_id is not None:
        if str(getattr(record, "engagement_id", "")) != str(engagement_id):
            return "other_run", None
    raw = raw_from_metadata(getattr(record, "metadata", None))
    if raw is None:
        return "no_raw", None
    return "ok", raw


def provenance_of(record) -> dict:
    if record is None:
        return {}
    return {
        "evidence_id": str(getattr(record, "evidence_id", "")),
        "run_id": str(getattr(record, "run_id", "")),
        "engagement_id": str(getattr(record, "engagement_id", "")),
        "action_id": str(getattr(record, "action_id", ""))
        if getattr(record, "action_id", None) is not None
        else None,
        "invocation_id": str(getattr(record, "invocation_id", ""))
        if getattr(record, "invocation_id", None) is not None
        else None,
        "hypothesis_id": str(getattr(record, "hypothesis_id", ""))
        if getattr(record, "hypothesis_id", None) is not None
        else None,
        "source_type": str(getattr(record, "source_type", ""))
        if getattr(record, "source_type", None) is not None
        else None,
        "source_id": getattr(record, "source_id", None),
    }


def classify_raw(record, requested_run_id: str) -> tuple:
    return locate_raw(record, requested_run_id)


async def read_raw_record(evidence_store, evidence_id: str, run_id: str) -> dict:
    record = await evidence_store.get(evidence_id)
    status, raw = classify_raw(record, run_id)
    base = {
        "status": status,
        "evidence_id": str(evidence_id),
        "requested_run_id": str(run_id),
        "raw": raw,
        "provenance": provenance_of(record),
        "truncated": False,
    }
    if status == "other_run" and record is not None:
        base["actual_run_id"] = str(record.run_id)
    if status in ("ok", "no_raw") and record is not None:
        base["summary"] = getattr(record, "summary", None)
    return base


class EvidenceReader:
    """Read back one evidence record of the current run, windowed."""

    def __init__(self, evidence_store: Any) -> None:
        self._store = evidence_store

    async def read(
        self,
        evidence_id: str,
        run_id: str,
        offset: int = 0,
        limit: int = ...,  # type: ignore[assignment]
        query: str | None = None,
    ) -> dict:
        """Return a window over the stored raw result.

        Status is one of "ok" / "not_found" / "run_mismatch" / "no_raw".
        Only "ok" carries "text"; a window cut short by offset/limit sets
        "truncated" instead of failing.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        record = await self._store.get(evidence_id)
        status, raw = locate_raw(record, run_id)
        if status == "missing":
            return {
                "status": "not_found",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "text": "",
                "truncated": False,
                "provenance": provenance_of(record),
            }
        if status == "other_run":
            return {
                "status": "run_mismatch",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "actual_run_id": str(record.run_id),
                "text": "",
                "truncated": False,
                "provenance": provenance_of(record),
            }
        if status == "no_raw" or raw is None:
            return {
                "status": "no_raw",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "summary": record.summary,
                "text": "",
                "truncated": False,
                "provenance": provenance_of(record),
            }
        total_chars = len(json.dumps(raw, ensure_ascii=False, default=str))
        full = json.dumps(raw, ensure_ascii=False, indent=2, default=str)
        match_count: int | None = None
        if query:
            needle = query.lower()
            hits = [line for line in full.splitlines() if needle in line.lower()]
            match_count = len(hits)
            full = "\n".join(hits)
        if limit is Ellipsis or limit is None:
            effective = DEFAULT_CONTEXT_CHARS
        else:
            effective = max(0, min(int(limit), ABSOLUTE_MAX_CHARS))
        window = full[offset : offset + effective] if effective else ""
        truncated = (offset + len(window)) < len(full)
        return {
            "status": "ok",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "summary": record.summary,
            "text": window,
            "truncated": truncated,
            "total_chars": total_chars,
            "filtered_chars": len(full),
            "offset": offset,
            "limit": effective,
            "query": query,
            "match_count": match_count,
            "provenance": provenance_of(record),
        }
