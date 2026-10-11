"""Explicit claim reuse conditions, separate from immutable historical origins.

The model proposes conditions; code grounds them. Current status is a fresh
projection, not a second authoritative evidence store or a success verdict.
No guessed expiry, source rewrite, Task resurrection or automatic target call.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType, MissionStatus
from redteam.core.ids import EvidenceId, new_id
from redteam.orchestration.coordination import StateConflict, content_hash, coordinator_for
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.knowledge import collect_measured_values
from redteam.orchestration.local_tools import LocalToolContext, LocalToolDescriptor, LocalToolResult
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.planning_admission import PlanningAdmission

BINDING_KIND = "evidence_validity_binding_v1"
SOURCE_ID = "orchestration.evidence_validity"
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


@dataclass(frozen=True)
class ValidityRead:
    run_id: str
    versions: tuple[tuple[str, int], ...]
    valid_until: datetime | None

    def check(self, tx) -> None:
        PlanningAdmission.check_references(tx, self.run_id, self.versions)
        if self.valid_until is not None and self.valid_until <= datetime.now(timezone.utc):
            raise StateConflict("completion_evidence_expired")


async def capture_validity(deps, runtime, evidence_ids) -> ValidityRead | None:
    """Code-owned relevant read set; called before validation/model review."""
    if getattr(deps, "specialist_policy", None) is None:
        return None
    coordinator = deps.coordinator or coordinator_for(deps.task_graph_store, deps.path_store)
    admission = PlanningAdmission(coordinator)
    run_id = str(runtime.run_id)
    # Include receipt origins, not just the mutable wrapper citation.
    identities = set(map(str, evidence_ids))
    for identity in tuple(identities):
        record = await deps.evidence_store.get(identity)
        if record is not None and str(record.run_id) == run_id:
            identities.update(map(str, record.metadata.get("source_evidence_ids", [])))
    versions = await admission.capture_references(run_id, [f"validity:{i}" for i in identities])
    records = await deps.evidence_store.list_for_run(run_id)
    nodes: set[str] = set()
    for record in records:
        if is_binding(record) and record.metadata.get("claim_evidence_id") in identities:
            try:
                binding = ReuseBinding.model_validate(record.metadata)
                nodes.update(f"node:{c.node_id}" for c in binding.conditions)
            except ValueError:
                pass  # original validation will reject this binding, not erase it
    node_versions = await admission.capture_references(run_id, nodes)
    current = [await current_validity(
        deps.evidence_store, AttackGraphService(deps.graph_store), run_id, identity,
        records=records,
    ) for identity in identities]
    read = ValidityRead(run_id, tuple(sorted((*versions, *node_versions))), min(
        (item.valid_until for item in current if item.valid_until is not None), default=None,
    ))
    async def verify(tx):
        # Expiry already observed by validation is a normal evidence gap.
        # The final settlement still checks wall-clock expiry atomically.
        admission.check_references(tx, run_id, read.versions)
    await coordinator.change(verify)
    return read


async def refresh_validity(deps, runtime) -> list[ValidityProjection]:
    """Deduplicate grounded status changes on the existing Coordinator/outbox."""
    run_id = str(runtime.run_id)
    coordinator = deps.coordinator or coordinator_for(deps.task_graph_store, deps.path_store)
    admission = PlanningAdmission(coordinator)
    # This projection scans the entire binding query, so include absence too.
    versions = await admission.capture_references(run_id, ("graph", "evidence"))
    current = await project_validity(
        deps.evidence_store, AttackGraphService(deps.graph_store), run_id,
    )

    async def record(tx):
        tx.admit(run_id, str(runtime.mission_id))
        admission.check_references(tx, run_id, versions)
        for item in current:
            if (item.status == "valid" and item.valid_until is not None
                    and item.valid_until <= datetime.now(timezone.utc)):
                raise StateConflict("validity_projection_expired")
            key = ("evidence_validity_projection", f"{run_id}:{item.evidence_id}")
            payload = item.model_dump(mode="json")
            if tx.records.get(key) == payload:
                continue
            tx.records[key] = payload
            tx.queue(EventEnvelope(
                event_id=new_id("event"), event_type=EventType.EVIDENCE_VALIDITY_CHANGED,
                run_id=runtime.run_id, engagement_id=runtime.engagement_id, source=SOURCE_ID,
                timestamp=datetime.now(timezone.utc), payload={"schema_version": "1", **payload},
            ))
    await coordinator.change(record)
    await deliver_pending(coordinator, run_id=run_id, engagement_id=str(runtime.engagement_id),
                          event_bus=deps.event_bus)
    return current


@dataclass(frozen=True)
class ValiditySchedule:
    """Notification hint only; expiry always requires fresh original reads."""

    digest: str
    next_expiry: datetime | None


async def validity_schedule(deps, runtime) -> ValiditySchedule | None:
    """Read observed deadlines in the existing records, never invent a TTL.

    Only an active specialist lifecycle may observe a wakeup. Held, paused,
    closed and pending operator mutations cannot start followup judgments.
    This is not a durable timer queue or evidence/permission authority.
    """
    coordinator = deps.coordinator or coordinator_for(deps.task_graph_store, deps.path_store)
    run_id, mission_id = str(runtime.run_id), str(runtime.mission_id)

    async def read(tx):
        bound = tx.binding(run_id, mission_id, str(runtime.engagement_id))
        mission = tx.tasks.peek_mission(runtime.mission_id)
        if (bound["closed"] or mission is None or mission.status is not MissionStatus.ACTIVE
                or tx.pending(run_id, mission_id)
                or tx.records.get(("specialist_lifecycle", run_id), {}).get("state") != "active"):
            return None
        projections = []
        for (kind, key), value in tx.records.items():
            if kind != "evidence_validity_projection" or not key.startswith(f"{run_id}:"):
                continue
            item = ValidityProjection.model_validate(value)
            if (key != f"{run_id}:{item.evidence_id}"
                    or (item.valid_until is not None and item.valid_until.tzinfo is None)):
                raise ValueError("validity_schedule_invalid")
            projections.append(item)
        projections.sort(key=lambda item: item.evidence_id)
        return ValiditySchedule(
            content_hash([item.model_dump(mode="json") for item in projections]),
            min((item.valid_until for item in projections
                 if item.status == "valid" and item.valid_until is not None), default=None),
        )

    return await coordinator.change(read)


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
            if await deps.evidence_store.get(identity) is None:
                await deps.evidence_store.append(EvidenceRecord(
                    evidence_id=identity, run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_type=EvidenceSourceType.SYSTEM, source_id=SOURCE_ID,
                    content_type="application/json", digest=content_hash(metadata),
                    metadata=metadata,
                    summary="Explicit reuse conditions, not a success verdict",
                    created_at=datetime.now(timezone.utc),
                ))
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
