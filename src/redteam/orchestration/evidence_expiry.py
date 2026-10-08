"""Grounded expiry timers on the existing coordinator/outbox, not a new scheduler.

Only exact cited claims of a held delegation can wake it. Pending deadlines
are recoverable from immutable bindings after a restart. Fired events are
deduplicated; a replay wakes only the original held fingerprint. Unknown TTLs
and unrelated evidence cannot create a timer. No terminal run auto-restarts.
"""

import asyncio
from datetime import datetime, timezone

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import MissionStatus
from redteam.core.ids import MissionId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import ChangeSet, CoordinationError, content_hash
from redteam.orchestration.delegation import DelegationState
from redteam.orchestration.delegation_decision import (
    TERMINATION_WORKER_HELD,
    DelegationGateInput,
    gate_delegation_decision,
)
from redteam.orchestration.evidence_validity import (
    ReuseCondition,
    _is_binding,
    condition_material,
    source_material,
)
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.reasoning_time import reasoning_time_allowed
from redteam.orchestration.state import RuntimeState

EXPIRY_EVENT = "EvidenceValidityExpired"


class EvidenceExpiryWaiter:
    def __init__(self, max_wait_seconds: float):
        if not 0 < max_wait_seconds <= 60:
            raise ValueError("validity_wait_limit_invalid")
        self.max_wait_seconds = max_wait_seconds

    async def wait_for_change(self, runtime: RuntimeState, deps: RuntimeDeps) -> bool:
        if deps.coordinator is None or runtime.mission_id is None or runtime.pending_approval_id:
            return False
        if not reasoning_time_allowed(deps.engagement, runtime):
            return False
        graph = AttackGraphService(deps.graph_store)
        run_id, mission_id = str(runtime.run_id), str(runtime.mission_id)
        engagement_id = str(runtime.engagement_id)
        publisher = EventPublisher(deps.event_bus, run_id=runtime.run_id,
                                   engagement_id=runtime.engagement_id, source="evidence.expiry")

        async def scope(change: ChangeSet) -> str:
            bound = change.binding(run_id, mission_id, engagement_id)
            mission = change.tasks.peek_mission(MissionId(mission_id))
            if bound["closed"] or mission is None or mission.status != MissionStatus.ACTIVE:
                raise CoordinationError("validity_wait_mission_closed")
            if change.pending(run_id, mission_id):
                return "operator"
            pointer = change.paths.peek_active_pointer(run_id)
            if pointer != (runtime.active_path_id, runtime.active_path_version):
                raise CoordinationError("validity_wait_path_changed")
            return "active"

        async def grounded(binding: EvidenceRecord) -> datetime | None:
            metadata = binding.metadata
            if (metadata.get("schema_version") != "1"
                or binding.digest != content_hash(metadata)
                or str(binding.engagement_id) != engagement_id):
                return None
            sources = await source_material(deps.evidence_store, run_id,
                                            metadata["claim_evidence_id"])
            if sources != metadata["sources"]:
                return None
            expiries: list[datetime] = []
            values = await condition_material(
                deps.evidence_store, graph, run_id,
                [ReuseCondition.model_validate(c) for c in metadata["conditions"]],
                engagement_id=engagement_id, expiries=expiries, check_expiry=False,
            )
            if values != metadata["values"] or not expiries:
                return None
            return min(expiries)

        try:
            if await deps.coordinator.change(scope) == "operator":
                return True  # OBSERVE owns explicit operator stop/pause application.
            records = await deps.evidence_store.list_for_run(runtime.run_id)
            candidates = []
            for state in runtime.delegations.values():
                if (state.status != "held" or state.termination_reason != TERMINATION_WORKER_HELD
                    or not state.held_fingerprint or state.path_id != runtime.active_path_id
                    or state.path_version != runtime.active_path_version):
                    continue
                gate = gate_delegation_decision(DelegationGateInput(
                    delegation=state.model_copy(update={"status": "active"}),
                    global_remaining_actions=runtime.remaining_budget,
                ))
                if not gate.investigate_model_allowed:
                    continue
                for binding in records:
                    if (_is_binding(binding)
                        and binding.metadata.get("claim_evidence_id") in state.evidence_ids):
                        deadline = await grounded(binding)
                        if deadline is None or not reasoning_time_allowed(
                            deps.engagement, runtime, now=deadline,
                        ):
                            continue
                        if state.budget.max_seconds is not None and (
                            (deadline - state.started_at).total_seconds()
                            >= state.budget.max_seconds
                        ):
                            continue
                        candidates.append((deadline, str(binding.evidence_id), binding, state))
            if not candidates:
                return False

            def timer_identity(identity: str, state: DelegationState) -> tuple[str, str]:
                return ("validity_timer", content_hash({
                    "run": run_id, "binding": identity, "delegation": state.delegation_id,
                    "path": state.path_id, "version": state.path_version,
                }))

            async def pending(change):
                await scope(change)
                return [c for c in candidates
                        if (timer := change.records.get(timer_identity(c[1], c[3]))) is None
                        or timer["held_fingerprint"] == c[3].held_fingerprint]

            candidates = await deps.coordinator.change(pending)
            if not candidates:
                return False
            deadline, identity, binding, state = min(
                candidates, key=lambda c: (c[0], c[1], c[3].delegation_id),
            )
            timer_key = timer_identity(identity, state)
            allowance_key = ("validity_wait_used", run_id)
            delay = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            async def reserve(change):
                if await scope(change) != "active":
                    return "operator"
                timer = change.records.get(timer_key)
                if timer is not None and timer["held_fingerprint"] != state.held_fingerprint:
                    return "settled"
                if timer is not None and timer["fired"]:
                    return "replay"
                used = change.records.get(allowance_key, 0.0)
                if delay > self.max_wait_seconds - used:
                    return "limit"
                # Reserving first means cancel/crash cannot replenish the wait allowance.
                change.records[allowance_key] = used + delay
                change.records[timer_key] = {
                    "run_id": run_id, "binding_id": identity, "deadline": deadline.isoformat(),
                    "held_fingerprint": state.held_fingerprint, "fired": False,
                }
                return "wait"

            disposition = await deps.coordinator.change(reserve)
            if disposition in {"settled", "limit"}:
                return False
            if disposition == "operator":
                return True
            if disposition != "replay":
                end = asyncio.get_running_loop().time() + delay
                while asyncio.get_running_loop().time() < end:
                    await asyncio.sleep(min(0.25, end - asyncio.get_running_loop().time()))
                    if await deps.coordinator.change(scope) == "operator":
                        return True
                    if (not reasoning_time_allowed(deps.engagement, runtime)
                        or await grounded(binding) != deadline):
                        return False
            if await grounded(binding) != deadline or datetime.now(timezone.utc) < deadline:
                return False

            async def fire(change):
                if await scope(change) != "active":
                    return False
                timer = change.records[timer_key]
                if not timer["fired"]:
                    change.queue(publisher.build(EXPIRY_EVENT, {
                        "schema_version": "1", "claim_evidence_id": binding.metadata[
                            "claim_evidence_id"], "binding_id": identity,
                        "delegation_id": state.delegation_id, "status": "stale",
                        "dedup_key": timer_key[1],
                    }))
                    timer["fired"] = True
                return True

            woke = await deps.coordinator.change(fire)
            await deliver_pending(deps.coordinator, run_id=run_id,
                                  engagement_id=engagement_id, event_bus=deps.event_bus)
            return woke
        except Exception:  # noqa: BLE001 - unreadable state must not wake/infer
            return False  # No guessed expiry or global evidence mutation on a bad read.
