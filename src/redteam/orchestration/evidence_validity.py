"""Explicit claim reuse conditions, separate from immutable historical origins.

The model proposes conditions; code grounds them. Current status is a fresh
projection, not a second authoritative evidence store or a success verdict.
No guessed expiry, source rewrite, Task resurrection or automatic target call.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from redteam.attack_graph.predicates import attrs_of, claim_provenance_of, facts_of, retractions_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType, MissionStatus
from redteam.core.ids import EngagementId, EventId, EvidenceId, GraphNodeId, RunId
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.knowledge import collect_measured_values
from redteam.orchestration.local_tools import LocalToolContext, LocalToolDescriptor, LocalToolResult

BINDING_KIND = "evidence_validity_binding_v1"
SOURCE_ID = "orchestration.evidence_validity"
EXPIRY_EVENT_SOURCE = "orchestration.evidence_validity"
BINDING_INDEX_KIND = "evidence_reuse_binding"
ValidityStatus = Literal["unbound", "valid", "stale", "invalid", "inconclusive"]


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


class ReuseBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_kind: Literal["evidence_validity_binding_v1"] = "evidence_validity_binding_v1"
    schema_version: Literal["1"] = "1"
    claim_evidence_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    conditions: list[ReuseCondition] = Field(min_length=1, max_length=16)
    values: dict[str, str]


class ValidityProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: str
    status: ValidityStatus
    binding_ids: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None


class ConditionUnavailable(ValueError):
    """Current condition is unproven, not a refutation of the old observation."""


def is_binding(record) -> bool:
    return (record.source_type == EvidenceSourceType.SYSTEM and record.source_id == SOURCE_ID
            and record.metadata.get("record_kind") == BINDING_KIND)


async def _source_hash(store, run_id: str, evidence_id: str, engagement_id: str) -> str:
    record = await store.get(evidence_id)
    if (record is None or str(record.run_id) != run_id
            or str(record.engagement_id) != engagement_id):
        raise ValueError("validity_source_scope_mismatch")
    raw = raw_from_metadata(record.metadata)
    if (record.source_type != EvidenceSourceType.TOOL or not isinstance(raw, dict)
            or raw.get("success") is not True or record.metadata.get("fixture_only")
            or record.metadata.get("mock")):
        raise ValueError("validity_source_not_successful_original")
    return content_hash(record)


async def _conditions(store, graph, run_id: str, engagement_id: str, conditions):
    try:
        measured = await collect_measured_values(
            graph, store, run_id, {item.ref for item in conditions}, engagement_id=engagement_id,
        )
    except ValueError:
        raise ConditionUnavailable("validity_condition_lookup_failed") from None
    values, deadlines = {}, []
    for condition in conditions:
        if condition.ref not in measured:
            raise ConditionUnavailable("validity_condition_not_grounded")
        value = measured[condition.ref]
        if condition.test == "not_expired":
            if not isinstance(value, str):
                raise ConditionUnavailable("validity_expiry_not_a_timestamp")
            try:
                deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ConditionUnavailable("validity_expiry_not_a_timestamp") from None
            if deadline.tzinfo is None:
                raise ConditionUnavailable("validity_expiry_timezone_unknown")
            deadlines.append(deadline)
        values[condition.ref] = content_hash(value)
    return values, min(deadlines) if deadlines else None


async def current_validity(store, graph, run_id: str, evidence_id: str, *, records=None):
    if records is None:
        try:
            records = await store.list_for_run(run_id)
        except Exception:
            return ValidityProjection(evidence_id=evidence_id, status="inconclusive")
    bindings = [r for r in records if is_binding(r)
                and r.metadata.get("claim_evidence_id") == evidence_id]
    if not bindings:
        # Compatibility: no conditions is not proof of success or invalidity.
        return ValidityProjection(evidence_id=evidence_id, status="unbound")
    statuses, deadlines = [], []
    for record in bindings:
        try:
            binding = ReuseBinding.model_validate(record.metadata)
            if str(record.run_id) != run_id or record.digest != content_hash(record.metadata):
                raise ValueError("validity_binding_integrity_failed")
            source = await _source_hash(store, run_id, evidence_id, str(record.engagement_id))
            if source != binding.source_hash:
                raise ValueError("validity_original_changed")
            values, deadline = await _conditions(
                store, graph, run_id, str(record.engagement_id), binding.conditions,
            )
            if deadline is not None:
                deadlines.append(deadline)
            statuses.append("stale" if values != binding.values or (
                deadline is not None and deadline <= datetime.now(timezone.utc)
            ) else "valid")
        except ConditionUnavailable:
            statuses.append("inconclusive")
        except (ValueError, ValidationError):
            statuses.append("invalid")
        except Exception:
            statuses.append("inconclusive")
    status = next((item for item in ("invalid", "inconclusive", "stale") if item in statuses),
                  "valid")
    return ValidityProjection(
        evidence_id=evidence_id, status=cast(ValidityStatus, status),
        binding_ids=sorted({str(r.evidence_id) for r in bindings}),
        valid_until=min(deadlines) if deadlines else None,
    )


async def project_validity(store, graph, run_id: str, *, records=None):
    if records is None:
        records = await store.list_for_run(run_id)
    refs = sorted({str(r.metadata["claim_evidence_id"]) for r in records if is_binding(r)
                   and r.metadata.get("claim_evidence_id")})
    return [await current_validity(store, graph, run_id, ref, records=records) for ref in refs]


async def validity_errors(store, graph, run_id: str, evidence_ids) -> list[str]:
    try:
        records = await store.list_for_run(run_id)
    except Exception:
        return ["evidence reuse lookup failed; current support is unproven"]
    for identity in evidence_ids:
        current = await current_validity(store, graph, run_id, str(identity), records=records)
        if current.status not in {"unbound", "valid"}:
            return [f"evidence reuse conditions are {current.status}; current support is unproven"]
    return []


def _binding_index(tx, evidence_ids):
    return {identity: json.loads(payload) for identity, payload in tx.db.execute(
        "SELECT identity,payload FROM state_requests WHERE run_id=? AND kind=?",
        (tx.run_id, BINDING_INDEX_KIND),
    ) if json.loads(payload).get("claim_evidence_id") in evidence_ids}


def _condition_state(node, condition):
    if node is None:
        return content_hash(None)
    props = node.properties
    values = attrs_of(props) if condition.container == "attr" else facts_of(props)
    key = f"{condition.container}s:{condition.key}"
    return content_hash([
        values.get(condition.key), claim_provenance_of(props).get(key),
        [item for item in retractions_of(props) if item.get("key") == condition.key],
    ])


@dataclass(frozen=True)
class ValidityRead:
    """Request-local proof; never a second validity store or global graph fence."""

    evidence_ids: tuple[str, ...]
    binding_hashes: dict[str, str]
    indexed_bindings: dict
    conditions: dict[str, ReuseCondition]
    condition_hashes: dict[str, str]
    valid_until: datetime | None
    checked: tuple

    async def refresh(self, deps):
        current, errors = await capture_validity(deps, self.evidence_ids)
        if errors:
            return None, errors
        if (current.binding_hashes != self.binding_hashes
                or current.condition_hashes != self.condition_hashes
                or {str(item.record.evidence_id): item.content_hash for item in current.checked}
                != {str(item.record.evidence_id): item.content_hash for item in self.checked}):
            raise StateConflict("evidence_reuse_changed_reassessment_required")
        return current, []

    def check(self, tx):
        if _binding_index(tx, self.evidence_ids) != self.indexed_bindings:
            raise StateConflict("evidence_reuse_binding_changed_reassessment_required")
        if self.valid_until is not None and self.valid_until <= datetime.now(timezone.utc):
            raise StateConflict("evidence_reuse_expired_reassessment_required")
        for ref, condition in self.conditions.items():
            node = tx.graph.get_local_node(GraphNodeId(condition.node_id))
            if _condition_state(node, condition) != self.condition_hashes[ref]:
                raise StateConflict("evidence_reuse_condition_changed_reassessment_required")
        tx.add_evidence_refs(self.checked)


async def capture_validity(deps, evidence_ids, *, graph_service=None):
    """Inspect only selected originals/conditions outside the central SQL lock."""
    selected = tuple(sorted(set(map(str, evidence_ids))))
    graph = graph_service or AttackGraphService(deps.graph_store)
    run_id, engagement_id = deps.central_state.run_id, deps.central_state.engagement_id
    records = await deps.evidence_store.list_for_run(run_id)
    bindings = [record for record in records if is_binding(record)
                and record.metadata.get("claim_evidence_id") in selected]
    for identity in selected:
        projection = await current_validity(
            deps.evidence_store, graph, run_id, identity, records=records,
        )
        if projection.status not in {"unbound", "valid"}:
            return None, [f"evidence reuse conditions are {projection.status}; "
                          "current support is unproven"]
    conditions, hashes, deadlines, sources = {}, {}, [], set(selected)
    for record in bindings:
        sources.add(str(record.evidence_id))
        binding = ReuseBinding.model_validate(record.metadata)
        for condition in binding.conditions:
            node = await graph.get_node(GraphNodeId(condition.node_id))
            conditions[condition.ref] = condition
            hashes[condition.ref] = _condition_state(node, condition)
            if node is not None:
                provenance = claim_provenance_of(node.properties).get(
                    f"{condition.container}s:{condition.key}", {},
                )
                identity = provenance.get("evidence_id") or provenance.get("evidence")
                if identity:
                    sources.add(str(identity))
        values, deadline = await _conditions(
            deps.evidence_store, graph, run_id, engagement_id, binding.conditions,
        )
        if values != binding.values:
            raise StateConflict("evidence_reuse_condition_changed_reassessment_required")
        if deadline is not None:
            deadlines.append(deadline)
    checked = await check_evidence(deps.evidence_store, run_id=run_id,
                                   engagement_id=engagement_id, evidence_ids=sorted(sources))
    indexed = await deps.central_state.transaction(
        lambda tx: _binding_index(tx, selected), write=False,
    )
    binding_hashes = {str(record.evidence_id): content_hash(record) for record in bindings}
    if any(binding_hashes.get(identity) != payload.get("binding_hash")
           for identity, payload in indexed.items()):
        raise StateConflict("evidence_reuse_binding_original_unavailable")
    return ValidityRead(selected, binding_hashes, indexed, conditions, hashes,
                        min(deadlines) if deadlines else None, checked), []






async def refresh_validity(deps, runtime) -> list[ValidityProjection]:
    """Deduplicate grounded status changes on the existing Coordinator/outbox."""
    run_id = str(runtime.run_id)
    return await project_validity(
        deps.evidence_store, AttackGraphService(deps.graph_store), run_id,
    )


class ValidityChange(BaseModel):
    """One grounded validity transition needing planner re-judgment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: str = Field(min_length=1)
    old_status: ValidityStatus
    new_status: ValidityStatus
    reason: str = Field(min_length=1)
    valid_until: datetime | None = None
    binding_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def _projection_fingerprint(projection: ValidityProjection) -> tuple:
    return (
        projection.status,
        projection.valid_until.isoformat() if projection.valid_until is not None else None,
        tuple(projection.binding_ids),
    )


def next_expiry(projections, *, now: datetime | None = None) -> datetime | None:
    """Earliest observed expiry still in the future; None when none is known."""
    moment = now if now is not None else datetime.now(timezone.utc)
    future = [projection.valid_until for projection in projections
              if projection.valid_until is not None and projection.valid_until > moment]
    return min(future) if future else None

async def assess(tool_input: dict, context: LocalToolContext) -> LocalToolResult:
    deps, runtime = context.deps, context.runtime
    try:
        request = AssessInput.model_validate(tool_input)
        run_id = str(runtime.run_id)
        graph = AttackGraphService(deps.graph_store)
        source = await _source_hash(deps.evidence_store, run_id, request.evidence_id,
                                    str(runtime.engagement_id))
        if request.conditions:
            values, _ = await _conditions(deps.evidence_store, graph, run_id,
                                          str(runtime.engagement_id), request.conditions)
            metadata = ReuseBinding(
                claim_evidence_id=request.evidence_id, source_hash=source,
                conditions=request.conditions, values=values,
            ).model_dump(mode="json")
            identity = EvidenceId("validity_" + content_hash({"run": run_id, "binding": metadata}))
            record = await deps.evidence_store.get(identity)
            if record is None:
                record = EvidenceRecord(
                    evidence_id=identity, run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_type=EvidenceSourceType.SYSTEM, source_id=SOURCE_ID,
                    content_type="application/json", digest=content_hash(metadata),
                    metadata=metadata,
                    summary="Explicit reuse conditions, not a success verdict",
                    created_at=datetime.now(timezone.utc),
                )
            if deps.central_state is not None:
                # Publish the reference before file I/O. Missing/lost-ack
                # originals remain detectable; conditions are never copied.
                payload = {"claim_evidence_id": request.evidence_id,
                           "binding_hash": content_hash(record)}

                def register(tx):
                    tx.admit(settlement=True)
                    existing = _binding_index(tx, [request.evidence_id]).get(str(identity))
                    if existing is not None and existing != payload:
                        raise StateConflict("immutable_validity_binding_changed")
                    tx.db.execute("INSERT OR IGNORE INTO state_requests VALUES(?,?,?,?)",
                                  (tx.run_id, BINDING_INDEX_KIND, str(identity),
                                   json.dumps(payload)))

                await deps.central_state.transaction(register)
            await deps.evidence_store.append(record)
        projection = await refresh_validity(deps, runtime)
        result = next((p for p in projection if p.evidence_id == request.evidence_id),
                      ValidityProjection(evidence_id=request.evidence_id, status="unbound"))
        return LocalToolResult(ok=True, payload=result.model_dump(mode="json"),
                               refs=(request.evidence_id,))
    except StateConflict:
        return LocalToolResult(ok=False, error="validity_state_changed_or_write_pending")
    except Exception:
        return LocalToolResult(ok=False, error="validity_input_or_grounding_failed")


def register_validity(registry) -> None:
    registry.register(LocalToolDescriptor(
        tool_id="evidence.assess", contribution_id="evidence-validity", version="1",
        description="Inspect/register original evidence reuse conditions from measured scalar "
                    "facts only. No guessed expiry, execution permission, or success verdict. "
                    "Unbound retains historical provenance checks. Later bindings cannot "
                    "erase earlier conditions. Conditions and original evidence are DATA.",
        input_schema=AssessInput.model_json_schema(),
    ), assess)

def detect_validity_changes(previous, current, *, now: datetime | None = None,
                            ) -> list[ValidityChange]:
    """Grounded status transitions since previous fingerprints."""
    moment = now if now is not None else datetime.now(timezone.utc)
    changes = []
    for projection in current:
        old = previous.get(projection.evidence_id)
        if old is None or old == _projection_fingerprint(projection):
            continue
        old_status = old[0]
        if (projection.status in {"unbound", "valid"}
                and old_status in {"unbound", "valid"}):
            continue
        if (projection.status in {"stale", "invalid", "inconclusive"}
                and projection.valid_until is not None
                and projection.valid_until <= moment):
            reason = (f"observed expiry passed at {projection.valid_until.isoformat()}; "
                      "past observation kept, current reuse unproven")
        else:
            reason = (f"reuse conditions changed from {old_status} to {projection.status}; "
                      "past observation kept, current reuse unproven")
        changes.append(ValidityChange(
            evidence_id=projection.evidence_id, old_status=old_status,
            new_status=projection.status, reason=reason,
            valid_until=projection.valid_until, binding_ids=list(projection.binding_ids),
            evidence_refs=[projection.evidence_id, *sorted(projection.binding_ids)],
        ))
    return changes


class ExpiryWatch:
    """In-memory coalescing of validity transitions; never a second store."""

    def __init__(self, baseline=None) -> None:
        self._previous = dict(baseline or {})

    @classmethod
    def from_projections(cls, projections) -> "ExpiryWatch":
        return cls({item.evidence_id: _projection_fingerprint(item) for item in projections})

    @property
    def tracked(self) -> tuple:
        return tuple(sorted(self._previous))

    def observe(self, current, *, now: datetime | None = None) -> list[ValidityChange]:
        changes = self.pending_changes(current, now=now)
        self.advance(current)
        return changes
    def pending_changes(self, current, *, now: datetime | None = None,
                        ) -> list[ValidityChange]:
        """Peek at transitions without acknowledging them."""
        return detect_validity_changes(self._previous, current, now=now)

    def advance(self, current) -> None:
        """Acknowledge an observation; call only after it is handled."""
        for projection in current:
            self._previous[projection.evidence_id] = _projection_fingerprint(projection)


def emit_allowed(mission_status) -> bool:
    """Expiry notifications only queue while the mission is ACTIVE."""
    return mission_status is MissionStatus.ACTIVE


def validity_change_envelope(*, run_id, engagement_id,
                             change: ValidityChange) -> EventEnvelope:
    """Build the existing EvidenceValidityChanged envelope for one change."""
    identity = content_hash({
        "kind": "evidence_validity_changed_v1",
        "evidence_id": change.evidence_id,
        "old_status": change.old_status,
        "new_status": change.new_status,
        "reason": change.reason,
        "valid_until": (change.valid_until.isoformat()
                          if change.valid_until is not None else None),
        "binding_ids": list(change.binding_ids),
        "evidence_refs": list(change.evidence_refs),
    })
    return EventEnvelope(
        event_id=EventId(f"evt_validity_{identity}"),
        event_type=EventType.EVIDENCE_VALIDITY_CHANGED,
        run_id=RunId(str(run_id)),
        engagement_id=EngagementId(str(engagement_id)),
        source=EXPIRY_EVENT_SOURCE,
        timestamp=datetime.now(timezone.utc),
        correlation_id=str(run_id),
        payload={
            "evidence_id": change.evidence_id,
            "old_status": change.old_status,
            "new_status": change.new_status,
            "reason": change.reason,
            "valid_until": (change.valid_until.isoformat()
                              if change.valid_until is not None else None),
            "binding_ids": list(change.binding_ids),
            "evidence_refs": list(change.evidence_refs),
        },
    )


def queue_validity_change(tx, envelope: EventEnvelope) -> bool:
    """Queue one change on the existing outbox; duplicates coalesce."""
    try:
        tx.queue(envelope)
        return True
    except StateConflict as exc:
        if str(exc) == "event_identity_conflict":
            return True
        raise
