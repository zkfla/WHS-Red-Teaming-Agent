"""Team-internal evidence reading for the orchestration loop.

The loop never hands a model the whole Evidence Store. EvidenceReader
serves one record of the *current* run at a time, with an explicit window
(offset/limit) and an optional substring filter (query), so a huge tool
output cannot blow up a prompt.

Raw ToolResult payloads live where VERIFY put them --
EvidenceRecord.metadata["tool_result"] -- there is no second store.
Three outcomes stay distinct on purpose: the record is missing
(not_found), it belongs to another run (run_mismatch), or it has no raw
payload (no_raw). Truncation is reported on the "ok" result itself
instead of being silent.
"""

from __future__ import annotations

import json
from typing import Any

from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier

__all__ = [
    "EvidenceReader",
    "StructuredLLMAnalysisVerifier",
    "DEFAULT_CONTEXT_CHARS",
    "ABSOLUTE_MAX_CHARS",
]

# Default context window (characters) when the caller passes no limit.
DEFAULT_CONTEXT_CHARS = 8000
# Hard ceiling even when the caller asks for more.
ABSOLUTE_MAX_CHARS = 20000
# Raw payload keys understood by the reader, newest first.
_RAW_KEYS = ("tool_result", "raw_tool_result")


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
        if record is None:
            return {
                "status": "not_found",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "text": "",
                "truncated": False,
            }
        if str(record.run_id) != str(run_id):
            return {
                "status": "run_mismatch",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "actual_run_id": str(record.run_id),
                "text": "",
                "truncated": False,
            }
        raw: Any = None
        for key in _RAW_KEYS:
            raw = record.metadata.get(key)
            if raw is not None:
                break
        if raw is None:
            return {
                "status": "no_raw",
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "summary": record.summary,
                "text": "",
                "truncated": False,
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
        }
