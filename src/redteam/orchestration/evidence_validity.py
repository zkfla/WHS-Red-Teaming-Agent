"""Append-only claim-scoped reuse conditions; historical observations stay intact.

No inferred TTL, source rewrite, graph reset, or self-issued Finding. Bindings
capture code-grounded graph values by digest; refresh reads the original store.
Unbound historical claims retain their existing validation rules.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import EngagementId, EvidenceId, RunId
from redteam.orchestration.analysis import source_origin_gap
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.knowledge import collect_measured_values
from redteam.orchestration.local_tools import LocalToolContext, LocalToolDescriptor, LocalToolResult

BINDING_KIND = "evidence_validity_binding_v1"
SOURCE_ID = "orchestration.evidence_validity"


def _is_binding(record) -> bool:
    return (record.source_type == EvidenceSourceType.SYSTEM and record.source_id == SOURCE_ID
            and record.metadata.get("record_kind") == BINDING_KIND)


class ReuseCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(min_length=1)
    container: Literal["attr", "fact"]
    key: str = Field(min_length=1)
    test: Literal["unchanged", "not_expired"] = "unchanged"

    @property
    def ref(self) -> str:
        return f"{self.node_id}#{self.container}:{self.key}"


class AssessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1)
    conditions: list[ReuseCondition] = Field(default_factory=list, max_length=16)


class ObservedExpiryElapsed(ValueError):
    """Only an explicitly grounded, timezone-aware expiry can trigger this."""


class InvalidOriginal(ValueError):
    """Known failed/mock/wrong-origin source, unlike an unavailable store read."""


async def source_material(store, run_id: str, evidence_id: str) -> dict[str, str]:
    """Successful original ToolResults only, including reviewed receipt origins."""
    from redteam.orchestration.completion import is_final_admission_receipt

    record = await store.get(evidence_id)
    if record is None or str(record.run_id) != run_id:
        raise InvalidOriginal("validity_source_not_in_run")
    refs = (record.metadata.get("source_evidence_ids", [])
            if is_final_admission_receipt(record) else [evidence_id])
    if not refs:
        raise InvalidOriginal("validity_original_sources_missing")
    sources = {evidence_id: record.digest}
    for reference in refs:
        original = await store.get(str(reference))
        if original is None or str(original.run_id) != run_id or (
            original.engagement_id != record.engagement_id
        ):
            raise InvalidOriginal("validity_source_not_in_run")
        raw = raw_from_metadata(original.metadata)
        if original.source_type != EvidenceSourceType.TOOL or (
            original.metadata.get("fixture_only") or original.metadata.get("mock")
        ) or source_origin_gap(original, raw) is not None or (
            not isinstance(raw, dict) or raw.get("success") is not True
        ):
            raise InvalidOriginal("validity_source_not_successful_original")
        sources[str(reference)] = content_hash({"digest": original.digest, "raw": raw})
    return sources


async def condition_material(
    store, graph, run_id: str, conditions, *, engagement_id: str | None = None,
    expiries: list[datetime] | None = None, check_expiry: bool = True,
) -> dict[str, str]:
    measured = await collect_measured_values(
        graph, store, run_id, {c.ref for c in conditions}, engagement_id=engagement_id,
    )
    values = {}
    for condition in conditions:
        if condition.ref not in measured:
            raise ValueError("validity_condition_not_grounded")
        if condition.test == "not_expired":
            value = measured[condition.ref]
            if not isinstance(value, str):
                raise ValueError("validity_expiry_not_a_timestamp")
            observed_expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if observed_expiry.tzinfo is None:
                raise ValueError("validity_expiry_timezone_unknown")
            if expiries is not None:
                expiries.append(observed_expiry)
            if check_expiry and observed_expiry <= datetime.now(timezone.utc):
                raise ObservedExpiryElapsed
        values[condition.ref] = content_hash(measured[condition.ref])
    return values


async def current_validity(
    store, graph, run_id: str, evidence_id: str, *, records=None,
) -> dict[str, Any]:
    """Fresh fail-closed projection, no write required for a completion check."""
    try:
        if records is None:
            records = await store.list_for_run(RunId(run_id))
        bindings = [r for r in records if _is_binding(r)
                    and r.metadata.get("claim_evidence_id") == evidence_id]
        if not bindings:
            return {
                "status": "unbound", "evidence_id": evidence_id,
                "meaning": (
                    "No explicit claim reuse conditions have been registered. This is not "
                    "a failed, expired, verified or successful source verdict. Historical "
                    "observations still use the existing original provenance, result and "
                    "completion checks; a reuse binding is not a prerequisite for every "
                    "observation. Control admission and evidence assessment are separate."
                ),
            }
        statuses = []
        for binding in bindings:
            metadata = binding.metadata
            # A narrower later binding cannot erase an earlier reuse condition.
            if metadata.get("schema_version") != "1" or binding.digest != content_hash(metadata):
                statuses.append("invalid")
                continue
            sources = await source_material(store, run_id, evidence_id)
            if sources != metadata["sources"]:
                statuses.append("invalid")
                continue
            conditions = [ReuseCondition.model_validate(c) for c in metadata["conditions"]]
            values = await condition_material(
                store, graph, run_id, conditions, engagement_id=str(binding.engagement_id),
            )
            statuses.append("valid" if values == metadata["values"] else "stale")
        status = next((s for s in ("invalid", "stale") if s in statuses), "valid")
        return {"status": status, "evidence_id": evidence_id,
                "binding_ids": [str(r.evidence_id) for r in bindings]}
    except ObservedExpiryElapsed:
        return {"status": "stale", "evidence_id": evidence_id}
    except InvalidOriginal:
        return {"status": "invalid", "evidence_id": evidence_id}
    except Exception:
        return {"status": "inconclusive", "evidence_id": evidence_id}


async def assess(tool_input: dict, context: LocalToolContext) -> LocalToolResult:
    try:
        request = AssessInput.model_validate(tool_input)
        run_id = str(context.runtime.run_id)
        store = context.deps.evidence_store
        graph = AttackGraphService(context.deps.graph_store)
        original = await store.get(request.evidence_id)
        if original is None or str(original.engagement_id) != str(context.runtime.engagement_id):
            raise ValueError("validity_source_engagement_mismatch")
        if request.conditions:
            sources = await source_material(store, run_id, request.evidence_id)
            values = await condition_material(
                store, graph, run_id, request.conditions,
                engagement_id=str(context.runtime.engagement_id),
            )
            metadata = {
                "record_kind": BINDING_KIND, "schema_version": "1",
                "claim_evidence_id": request.evidence_id, "sources": sources,
                "conditions": [c.model_dump() for c in request.conditions], "values": values,
            }
            digest = content_hash(metadata)
            identity = EvidenceId("validity_" + content_hash({"run": run_id, "value": metadata}))
            if await store.get(identity) is None:
                await store.append(EvidenceRecord(
                    evidence_id=identity, run_id=RunId(run_id),
                    engagement_id=EngagementId(str(context.runtime.engagement_id)),
                    source_type=EvidenceSourceType.SYSTEM,
                    source_id=SOURCE_ID,
                    content_type="application/json", digest=digest, metadata=metadata,
                    summary="Explicit reuse conditions; not a vulnerability verdict",
                    created_at=datetime.now(timezone.utc),
                ))
        else:
            await source_material(store, run_id, request.evidence_id)
        result = await current_validity(store, graph, run_id, request.evidence_id)
        return LocalToolResult(ok=True, payload=result, refs=(request.evidence_id,))
    except Exception:
        return LocalToolResult(ok=False, error="validity_input_or_grounding_failed")


async def project_validity(store, graph, run_id: str, records=None) -> list[dict[str, Any]]:
    """Shared fresh projection for decision context and reporting, never a second store."""
    if records is None:
        records = await store.list_for_run(RunId(run_id))
    refs = sorted({str(r.metadata["claim_evidence_id"]) for r in records
                   if _is_binding(r)
                   and r.metadata.get("claim_evidence_id")})
    return [await current_validity(store, graph, run_id, ref, records=records) for ref in refs]


def register_validity(registry) -> None:
    registry.register(LocalToolDescriptor(
        tool_id="evidence.assess", contribution_id="evidence-validity",
        description=("Check an original/receipt's current explicit reuse conditions. "
                     "Optional grounded session/version facts; no guessed expiry. "
                     "Unbound means no registered reuse conditions, not invalid evidence. "
                     "Historical original/completion checks still apply. "
                     "Validity is not Finding or goal success."),
        input_schema=AssessInput.model_json_schema(),
    ), assess)
