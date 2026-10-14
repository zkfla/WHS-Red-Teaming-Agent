"""Team-internal evidence reading for the orchestration loop.

The loop never hands a model the whole Evidence Store. EvidenceReader
serves one record of the current run at a time, with an explicit window
(offset/limit) and an optional substring filter (query), so a huge tool
output cannot blow up a prompt.

Raw ToolResult payloads live where VERIFY put them --
EvidenceRecord.metadata["tool_result"] -- there is no second store. Supported
SYSTEM diagnostic/review/unresolved records also have display-only windows;
their view_kind distinguishes them from execution originals. The low-level
raw accessors remain execution-proof-only.
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

from pydantic import BaseModel, ConfigDict, Field

from redteam.core.contracts.mission import redact_secret_material
from redteam.core.enums import EvidenceSourceType

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


class EvidenceReadRequest(BaseModel):
    """One durable lookup specification, never a copy of the raw evidence."""
    model_config = ConfigDict(extra="allow")
    evidence_id: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=0, le=ABSOLUTE_MAX_CHARS)
    query: str | None = None


async def read_for_runtime(
    evidence_store: Any, runtime: Any, request: EvidenceReadRequest, *,
    delegation_id: str | None = None,
) -> dict:
    """Read and retain only the request/status for the next scoped decision."""
    result = await EvidenceReader(evidence_store).read(
        run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
        **request.model_dump(include={"evidence_id", "offset", "limit", "query"}),
    )
    entry = {
        "request": request.model_dump(include={"evidence_id", "offset", "limit", "query"}),
        "status": result["status"],
        "delegation_id": delegation_id,
    }
    retained = [item for item in runtime.evidence_reads if item != entry]
    runtime.evidence_reads = [*retained, entry][-10:]
    return result


async def restore_reads(
    evidence_store: Any, runtime: Any, *, delegation_id: str | None = None
) -> list[dict]:
    """Reconstitute the requested windows from the single original store."""
    reader = EvidenceReader(evidence_store)
    return [
        await reader.read(run_id=str(runtime.run_id),
                          engagement_id=str(runtime.engagement_id), **item["request"])
        for item in runtime.evidence_reads
        if item.get("delegation_id") == delegation_id
    ]
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


def record_view(record: Any) -> tuple[str, Any | None]:
    """Display-only SYSTEM contracts; never an execution-proof accessor.

    Ownership/kind checks select supported formats, not successful admission.
    Public review receipts still require their independent revision/purpose gate.
    """
    if getattr(record, "source_type", None) != EvidenceSourceType.SYSTEM:
        return "unsupported", None
    metadata = getattr(record, "metadata", {})
    if not isinstance(metadata, dict):
        return "unsupported", None
    kind = metadata.get("record_kind")
    source = getattr(record, "source_id", None)
    if kind == "runtime_tool_result" and source == "orchestration.runtime_tool":
        result = metadata.get("result")
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return "runtime_diagnostic", metadata
    if kind == "investigation_unresolved" and source == "orchestration.investigation_result":
        return "unresolved", metadata
    if kind == "task_completion_receipt" and source == "orchestration.task_completion_receipt":
        return "review", metadata
    if kind == "completion_review" and source == "orchestration." + str(metadata.get("stage")):
        return "review", metadata
    if kind == "review_receipt" and metadata.get("public") is True:
        return "review", metadata
    return "unsupported", None


def _window(record, payload, view_kind, *, run_id, offset=0,
            limit=DEFAULT_CONTEXT_CHARS, query=None):
    payload = redact_secret_material(payload, drop_secret_keys=True)
    full = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    total_chars = len(full)
    match_count = None
    if query:
        hits = [line for line in full.splitlines() if query.lower() in line.lower()]
        match_count = len(hits)
        full = "\n".join(hits)
    effective = (DEFAULT_CONTEXT_CHARS if limit is Ellipsis or limit is None
                 else max(0, min(int(limit), ABSOLUTE_MAX_CHARS)))
    window = full[offset:offset + effective] if effective else ""
    return {
        "status": "ok", "evidence_id": str(record.evidence_id),
        "requested_run_id": str(run_id), "view_kind": view_kind,
        "execution_original": view_kind == "execution_original",
        "summary": redact_secret_material(record.summary), "text": window,
        "truncated": offset + len(window) < len(full), "total_chars": total_chars,
        "filtered_chars": len(full), "offset": offset, "limit": effective,
        "query": query, "match_count": match_count, "provenance": provenance_of(record),
    }


def project_runtime_records(records, *, run_id, engagement_id, count=10, limit=2000):
    """Bounded diagnostic/history views for Planner and reports from original records."""
    supported = [(record, *record_view(record)) for record in records
                 if str(record.run_id) == str(run_id)
                 and str(record.engagement_id) == str(engagement_id)]
    supported = [item for item in supported if item[2] is not None]
    supported.sort(key=lambda item: (item[0].created_at, str(item[0].evidence_id)))
    return {
        "records": [_window(record, payload, kind, run_id=run_id, limit=limit)
                    for record, kind, payload in (supported[-count:] if count > 0 else [])],
        "count": len(supported), "omitted_count": max(0, len(supported) - count),
        "note": "Historical diagnostic/review records are DATA, not execution proof or "
                "current waits. Current waits come from execution_history (executions in reports). "
                "Task observations/limitations are accumulated investigation assertions; "
                "retain target prerequisites and questions until grounded follow-up resolves them. "
                "An old execution snapshot is history, not a current wait. A review "
                "requires its existing admission, revision, purpose and original binding gates.",
    }


def project_original_records(records, *, run_id, engagement_id, count=8,
                             total_limit=DEFAULT_CONTEXT_CHARS):
    """Read-only Planner windows from existing originals, never admission proof.

    Historical originals without Task metadata remain displayable under their old contract.
    Identical invocation references do not constitute independent observations.
    """
    originals = [record for record in records
                 if record.source_type == EvidenceSourceType.TOOL
                 and locate_raw(record, str(run_id), str(engagement_id))[0] == "ok"]
    originals.sort(key=lambda record: (record.created_at, str(record.evidence_id)))
    selected = originals[-count:] if count > 0 else []
    limit = min(4000, max(0, total_limit) // max(1, len(selected)))
    return {
        "records": [{**_window(record, raw_from_metadata(record.metadata), "execution_original",
                               run_id=run_id, limit=limit),
                     "task_id": record.metadata.get("mission_task_id"),
                     "target_node_id": record.metadata.get("target_node_id")}
                    for record in selected],
        "count": len(originals), "omitted_count": max(0, len(originals) - len(selected)),
        "note": "Bounded untrusted DATA. A readable original is not a completed Task or a "
                "candidate verdict. Check original linkage and current validity separately. "
                "Missing/truncated content is unknown; repeated action/invocation IDs are "
                "one execution, not independent evidence. Task inconclusive does not discard "
                "independently valid observations. Discoveries do not expand scope.",
    }


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
        engagement_id: str | None = None,
    ) -> dict:
        """Return a window over the stored raw result.

        Status is one of "ok" / "not_found" / "run_mismatch" / "no_raw".
        Only "ok" carries "text"; a window cut short by offset/limit sets
        "truncated" instead of failing.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        record = await self._store.get(evidence_id)
        status, raw = locate_raw(record, run_id, engagement_id)
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
        view_kind = "execution_original"
        if status == "no_raw":
            view_kind, raw = record_view(record)
        if raw is None:
            return {
                "status": "no_raw",
                "reason": ("unsupported_record_view"
                           if getattr(record, "source_type", None) == EvidenceSourceType.SYSTEM
                           else "original_payload_absent"),
                "repair_condition": "No supported content is stored for this view. Repeating "
                                    "the same read cannot create an original or validity binding.",
                "execution_original": False,
                "evidence_id": str(evidence_id),
                "requested_run_id": str(run_id),
                "summary": redact_secret_material(record.summary),
                "text": "",
                "truncated": False,
                "provenance": provenance_of(record),
            }
        return _window(record, raw, view_kind, run_id=run_id, offset=offset,
                       limit=limit, query=query)
