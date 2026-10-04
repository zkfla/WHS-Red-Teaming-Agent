"""Processing and recovery regressions (plan 3.5.4, D ownership).

Production paths only (no network/clock). Covers the worker-hold wake rule
(new related raw/references wake a held investigation; duplicate record ids
never do) and the reprocessing contract (same-source attempts project
start+final, conflicting endings read unknown, originals stay untouched,
tools are never re-invoked).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import ActionId, EngagementId, EvidenceId, InvocationId, RunId
from redteam.orchestration.analysis import DeterministicAnalysisVerifier
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.context_projection import project_run_processing
from redteam.orchestration.delegated_runtime import _worker_hold_fingerprint
from redteam.orchestration.delegation import DelegationRequest
from redteam.orchestration.delegation_decision import TERMINATION_WORKER_HELD, open_delegation
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.observations import default_result_mapper_registry
from redteam.orchestration.recovery import (
    ProcessingAttempt,
    mapping_attempt_key,
    record_processing_attempt,
    reprocess_evidence,
)
from tests.integration import _engine_llm_harness as h
from tests.unit.test_autonomous_delegation import MinimalLLM, _real_deps, minimal_draft

ENGAGEMENT = "eng-t1"


def _linked_evidence(run_id, evidence_id, node_id, output):
    record = h.make_tool_evidence(
        evidence_id=evidence_id, run_id=run_id, engagement_id=ENGAGEMENT,
        action_id="action-hold-1", invocation_id="inv-hold-1",
        path_id="path-hold", path_version=1, step_id="step-1",
        target_node_id=node_id, output=output,
    )
    record.metadata["proposal"] = {
        **record.metadata["proposal"], "run_id": run_id, "engagement_id": ENGAGEMENT,
    }
    return record


def _replayable_evidence(run_id, evidence_id, action_id, invocation_id, host):
    proposal, result = h.make_proposal_result(
        run_id=run_id, engagement_id=ENGAGEMENT,
        action_id=action_id, invocation_id=invocation_id,
        host=host, output={"inventory": "ok"},
    )
    target_id = str(node_id_for_target(proposal.target))
    metadata = {
        "path_id": "path-reproc", "path_version": 1, "path_step_id": "step-1",
        "target_node_id": target_id, "capability": "recon",
        "operation": "service_discovery", "canonical_operation": "recon.service_discovery",
        "operation_key": "recon.service_discovery",
        "action_id": action_id, "invocation_id": invocation_id, "success": True,
        "proposal": proposal.model_dump(mode="json"),
        "tool_result": result.model_dump(mode="json"),
    }
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id), run_id=RunId(run_id),
        engagement_id=EngagementId(ENGAGEMENT), source_type=EvidenceSourceType.TOOL,
        source_id=h.INVENTORY_TOOL, action_id=ActionId(action_id),
        invocation_id=InvocationId(invocation_id), content_type="application/json",
        digest=hashlib.sha256(evidence_id.encode()).hexdigest(),
        summary="replayable harness tool evidence",
        created_at=datetime.now(timezone.utc), metadata=metadata,
    )


async def _held_setup(run_id):
    llm = MinimalLLM()
    seed = await h.seed_host(run_id)
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=5)
    deps, _ = _real_deps(llm, seed=seed, world=world)
    engine = InvestigationEngine(deps)
    llm.add("path_draft", minimal_draft(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", h.turn_actions(seed.node_id))
    await engine.decide(h.make_runtime(run_id, ENGAGEMENT), GraphObservation())
    runtime = engine.updated_runtime
    path = await world.paths.get_active(run_id)
    assert path is not None
    state = open_delegation(
        DelegationRequest(
            path_step_id="step-1", agent_id=h.RECON_AGENT, objective="map inventory"
        ),
        path_id=path.path_id, path_version=path.version, decision_limit=5,
    )
    return seed, world, engine, runtime, state


async def test_hold_wakes_on_related_raw_not_on_duplicate_ids() -> None:
    run_id = "run-hold-wake"
    seed, world, engine, runtime, state = await _held_setup(run_id)
    baseline = await _worker_hold_fingerprint(engine, runtime, state)
    assert baseline is not None
    held = state.model_copy(
        update={"status": "held", "termination_reason": TERMINATION_WORKER_HELD,
                "held_fingerprint": baseline},
    )
    assert await _worker_hold_fingerprint(engine, runtime, held) == baseline
    await world.evidence.append(
        _linked_evidence(run_id, "ev-hold-1", seed.node_id, {"inventory": "host-a"})
    )
    woken = await _worker_hold_fingerprint(engine, runtime, held)
    assert woken is not None
    assert woken != baseline
    advanced = held.model_copy(update={"held_fingerprint": woken})
    await world.evidence.append(
        _linked_evidence(run_id, "ev-hold-2", seed.node_id, {"inventory": "host-a"})
    )
    assert await _worker_hold_fingerprint(engine, runtime, advanced) == woken
    await world.evidence.append(
        _linked_evidence(run_id, "ev-hold-3", "host:other.local", {"inventory": "host-b"})
    )
    assert await _worker_hold_fingerprint(engine, runtime, advanced) == woken
    await world.evidence.append(
        _linked_evidence(run_id, "ev-hold-4", seed.node_id, {"inventory": "host-a-changed"})
    )
    assert await _worker_hold_fingerprint(engine, runtime, advanced) != woken


async def test_same_source_attempts_project_start_final_and_conflict_unknown() -> None:
    run_id = "run-proc-attempts"
    llm = MinimalLLM()
    world = h.make_world(llm, decision_max_attempts=5)
    source = h.make_tool_evidence(
        evidence_id="ev-proc-src", run_id=run_id, engagement_id=ENGAGEMENT,
        action_id="action-proc-1", invocation_id="inv-proc-1",
        path_id="path-proc", path_version=1, step_id="step-1",
        target_node_id="host:a.local",
    )
    raw_before = dict(source.metadata["tool_result"])
    await world.evidence.append(source)
    first_key = mapping_attempt_key("ev-proc-src", "action-proc-1", "inv-proc-1", 1)
    assert first_key == mapping_attempt_key("ev-proc-src", "action-proc-1", "inv-proc-1", 1)
    assert first_key != mapping_attempt_key("ev-proc-src", "action-proc-1", "inv-proc-1", 2)
    assert "ev-proc-companion" not in first_key
    await record_processing_attempt(
        evidence_store=world.evidence, run_id=run_id, engagement_id=ENGAGEMENT,
        source_evidence_id="ev-proc-src", tool_id=h.INVENTORY_TOOL,
        processing=ProcessingAttempt(status="attempted", stage="nodes", attempted_nodes=2),
        action_id="action-proc-1", invocation_id="inv-proc-1", attempt=1,
    )
    await record_processing_attempt(
        evidence_store=world.evidence, run_id=run_id, engagement_id=ENGAGEMENT,
        source_evidence_id="ev-proc-src", tool_id=h.INVENTORY_TOOL,
        processing=ProcessingAttempt(status="confirmed", stage="done",
                                     attempted_nodes=2, confirmed_nodes=2),
        action_id="action-proc-1", invocation_id="inv-proc-1",
        attempt=2, supersedes_attempt=1,
    )
    records = await world.evidence.list_for_run(run_id)
    view = project_run_processing(records)["ev-proc-src"]
    assert view["processing_attempt"] == 2
    assert view["processing_status"] == "confirmed"
    assert view["processing_stage"] == "done"
    assert view["processing_conflict"] is False
    await record_processing_attempt(
        evidence_store=world.evidence, run_id=run_id, engagement_id=ENGAGEMENT,
        source_evidence_id="ev-proc-src", tool_id=h.INVENTORY_TOOL,
        processing=ProcessingAttempt(status="failed", stage="edges",
                                     attempted_edges=1, reason="boom"),
        action_id="action-proc-1", invocation_id="inv-proc-1", attempt=2,
    )
    conflicted = project_run_processing(await world.evidence.list_for_run(run_id))
    assert conflicted["ev-proc-src"]["processing_status"] == "unknown"
    assert conflicted["ev-proc-src"]["processing_conflict"] is True
    stored = await world.evidence.get("ev-proc-src")
    assert stored is not None
    assert stored.metadata["tool_result"] == raw_before
    assert not any(key.startswith("processing_") for key in stored.metadata)


async def test_reprocess_replays_stored_payload_without_tool_call() -> None:
    run_id = "run-reprocess"
    llm = MinimalLLM()
    seed = await h.seed_host(run_id)
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=5)
    source = _replayable_evidence(
        run_id, "ev-reproc-src", "action-reproc-1", "inv-reproc-1", h.HOST,
    )
    await world.evidence.append(source)
    before = await world.evidence.list_for_run(run_id)
    assert [record.source_type for record in before] == [EvidenceSourceType.TOOL]
    service = AttackGraphService(seed.store)
    result = await reprocess_evidence(
        evidence_id="ev-reproc-src", run_id=run_id, evidence_store=world.evidence,
        graph_service=service, verifier=DeterministicAnalysisVerifier(),
        result_mappers=default_result_mapper_registry(), budget=DecisionBudget(3),
    )
    assert result["status"] == "ok"
    after = await world.evidence.list_for_run(run_id)
    assert len(after) > len(before)
    assert {record.source_type for record in after} <= {
        EvidenceSourceType.TOOL, EvidenceSourceType.SYSTEM,
    }
    assert all(
        record.source_type is EvidenceSourceType.SYSTEM
        for record in after if str(record.evidence_id) != "ev-reproc-src"
    )
    stored = await world.evidence.get("ev-reproc-src")
    assert stored is not None
    assert stored.metadata["tool_result"]["output"] == {"inventory": "ok"}
    assert result["evidence_id"] == "ev-reproc-src"
