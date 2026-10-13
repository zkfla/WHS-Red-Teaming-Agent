"""plan5a: temporary SQLite, synthetic originals, no model/provider execution."""

import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.hypotheses import HypothesisConflict
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.mission import InvestigationResult, MissionTask
from redteam.core.enums import GraphNodeType
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.central_completion import goal_inputs, goal_snapshot
from redteam.orchestration.central_results import result_for
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.unit.test_central_state import BINDING, ENG, MISSION, RUN, original, task, world


async def setup(tmp_path):
    state = await world(tmp_path)
    graph = build_central_graph(state)
    service = AttackGraphService(graph)
    await service.upsert_node(GraphNodeType.HOST, key="a", facts={"reachable": True})
    agents = AgentRegistry()
    agents.register_definition(AgentDefinition(agent_id="probe-role", name="Synthetic executor",
                                              role="executor", provider_id="synthetic",
                                              context_profile=ContextProfile(profile_id="synthetic"),
                                              capabilities=["synthetic"], schedulable=True))
    agents.register(AgentSpec(agent_id="probe-role", name="Synthetic executor",
                              capabilities=["synthetic"]))
    deps = SimpleNamespace(central_state=state, graph_store=graph, agents=agents,
                           evidence_store=FilesystemEvidenceStore(tmp_path / "evidence"))
    return deps, service


def candidate(identity="candidate-a", *, revision=None, status="open", evidence_ids=()):
    return {"expected_revision": revision, "hypothesis": {
        "contract_version": 2, "hypothesis_id": identity, "description": "Access may be unguarded",
        "target_node_ids": ["host:a"], "reason": "Verify the observed response",
        "status": status, "supporting_evidence_ids": list(evidence_ids),
    }}


def probe(key="probe-a", *, revision=1, discovery=False, **extra):
    return {"task_key": key, "objective": "Inspect one response", "title": "Response check",
            "role_id": "probe-role", "target_node_ids": ["host:a"],
            "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
            "hypothesis_revisions": {} if discovery else {"candidate-a": revision}, **extra}


async def admit(
    deps, service, request_id, *, updates=(), tasks=(), graph_read=None, task_read=None,
):
    if graph_read is None:
        identities = {"host:a"}
        for update in updates:
            if update.get("expected_revision") is not None:
                identities.add("hypothesis:" + update["hypothesis"]["hypothesis_id"])
        for item in tasks:
            updated_ids = {u["hypothesis"]["hypothesis_id"] for u in updates}
            identities.update("hypothesis:" + identity
                              for identity in item["hypothesis_revisions"]
                              if identity not in updated_ids)
        graph_read = {identity: content_hash(await service.get_node(identity))
                      for identity in identities}
    return await register_probe_tasks(
        deps, proposal_id=request_id,
        proposal=HypothesisProbeProposal.model_validate({"updates": list(updates),
                                                         "tasks": list(tasks)}),
        expected_revision=1, expected_binding_hash=BINDING, graph_read=graph_read,
        known_targets={"host:a"}, known_principals=set(), max_tasks=4,
        task_read=task_read if task_read is not None else {
            item["source_task_id"]: content_hash(await deps.central_state.get_task(
                item["source_task_id"])) for item in tasks if item.get("source_task_id")
        },
    )


async def test_candidate_revisions_two_probes_and_exact_replay(tmp_path):
    deps, service = await setup(tmp_path)
    first = await admit(deps, service, "first", updates=[candidate()], tasks=[probe()])
    replay = await admit(deps, service, "first", updates=[candidate()], tasks=[probe()])
    assert replay == first
    assert (await service.get_hypothesis("candidate-a")).revision == 1
    claimed = await deps.central_state.claim_task(first["task_ids"][0], owner="worker-a",
                                                instance_id="instance-a")
    checked = await original(deps.evidence_store, "receipt-a")
    result = result_for(claimed, outcome="inconclusive", summary="A response was observed",
                        evidence_ids=["receipt-a"], observations=["Anonymous response received"],
                        limitations=["Other accounts were not tested"],
                        hypothesis_ids=["candidate-a"])
    await deps.central_state.transaction(lambda tx: tx.finish_task(
        str(claimed.task_id), result, owner="worker-a", version=claimed.ownership_version,
        checked=checked,
    ))
    next_probe = probe("probe-b", revision=2, source_task_id=str(claimed.task_id),
                       source_result_id=result.result_id, evidence_ids=["receipt-a"])
    second = await admit(deps, service, "second",
                         updates=[candidate(revision=1, evidence_ids=["receipt-a"])],
                         tasks=[next_probe])
    assert first["task_ids"] != second["task_ids"]
    saved = await service.get_hypothesis("candidate-a")
    assert saved.revision == 2 and saved.status == "open"
    assert saved.supporting_evidence_ids == ["receipt-a"]
    followup = await deps.central_state.get_task(second["task_ids"][0])
    assert followup.source_question_id is None and followup.source_result_id == result.result_id
    assert not followup.required and not followup.goal_hypothesis_ids()
    # Same evidence and candidate are legal inputs for a different registration.
    third = await admit(deps, service, "third", tasks=[
        probe("probe-c", revision=2, evidence_ids=["receipt-a"]),
    ])
    assert third["task_ids"] != second["task_ids"]
    links = await service.incident_edges("hypothesis:candidate-a")
    assert any(str(edge.target_id) == "host:a" for edge in links)


@pytest.mark.parametrize("failure_table", ["state_tasks", "state_outbox", "state_requests"])
async def test_candidate_and_task_rollback_then_retry(tmp_path, failure_table):
    deps, service = await setup(tmp_path)
    with sqlite3.connect(deps.central_state.path) as db:
        db.execute(f"CREATE TRIGGER failed_write BEFORE INSERT ON {failure_table} "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        await admit(deps, service, "atomic", updates=[candidate()], tasks=[probe()])
    assert await service.get_hypothesis("candidate-a") is None
    assert await deps.central_state.list_tasks() == []
    with sqlite3.connect(deps.central_state.path) as db:
        db.execute("DROP TRIGGER failed_write")
    receipt = await admit(deps, service, "atomic", updates=[candidate()], tasks=[probe()])
    assert receipt["candidate_revisions"] == {"candidate-a": 1}


async def test_related_cas_and_legacy_writers_cannot_overwrite_candidate(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "create", updates=[candidate()])
    read = {identity: content_hash(await service.get_node(identity))
            for identity in ("host:a", "hypothesis:candidate-a")}
    await service.upsert_node(GraphNodeType.HOST, key="unrelated", facts={"new": True})
    await admit(deps, service, "update", updates=[candidate(revision=1, status="held")],
                graph_read=read)
    with pytest.raises(StateConflict, match="related_graph_changed"):
        await admit(deps, service, "stale", updates=[candidate(revision=1)], graph_read=read)
    for write in (
        service.resolve_hypothesis("candidate-a", status="confirmed"),
        service.record_hypothesis(Hypothesis(hypothesis_id="candidate-a", description="Overwrite")),
        service.upsert_node(GraphNodeType.HYPOTHESIS, node_id="hypothesis:candidate-a",
                            facts={"status": "confirmed"}),
        service.remove_node("hypothesis:candidate-a"),
    ):
        with pytest.raises(HypothesisConflict):
            await write
    assert (await service.get_hypothesis("candidate-a")).status == "held"


async def test_discovery_and_goal_relevance_are_not_candidate_verdicts(tmp_path):
    deps, service = await setup(tmp_path)
    receipt = await admit(deps, service, "observe", updates=[candidate()], tasks=[
        probe(required=True, required_reason="The goal requires a response observation"),
        probe("discovery", discovery=True),
    ])
    first = await deps.central_state.get_task(receipt["task_ids"][0])
    mission, tasks, stamp = await deps.central_state.transaction(goal_snapshot, write=False)
    assert first.required and first.related_hypothesis_ids and not first.goal_hypothesis_ids()
    # Changing this context-only candidate must not change the goal's required graph snapshot.
    await admit(deps, service, "hold", updates=[candidate(revision=1, status="held")])
    assert stamp == (await deps.central_state.transaction(goal_snapshot, write=False))[2]
    runtime = SimpleNamespace(run_id=RUN, engagement_id=ENG, mission_id=MISSION)
    errors = (await goal_inputs(deps, runtime))[3]
    assert "required_investigation_unfinished" in errors
    assert "goal_required_hypotheses_unresolved" not in errors
    legacy = task().model_copy(update={"related_hypothesis_ids": ["candidate-a"]})
    assert legacy.goal_hypothesis_ids() == ["candidate-a"]
    await admit(deps, service, "goal-proof", tasks=[probe(
        "goal-proof", revision=2, required=True,
        required_reason="Goal needs this candidate verdict",
        goal_required_hypothesis_ids=["candidate-a"],
    )])
    assert "goal_required_hypotheses_unresolved" in (await goal_inputs(deps, runtime))[3]


async def test_result_versions_and_original_observation_retention(tmp_path):
    deps, service = await setup(tmp_path)
    receipt = await admit(deps, service, "first", updates=[candidate()], tasks=[probe()])
    claimed = await deps.central_state.claim_task(receipt["task_ids"][0], owner="worker-a",
                                                instance_id="instance-a")
    result = result_for(claimed, outcome="inconclusive", summary="Partial check",
                        evidence_ids=[], observations=["Access rejected"],
                        limitations=["No authenticated session"])
    saved = await deps.central_state.transaction(lambda tx: tx.publish_result(
        str(claimed.task_id), result, owner="worker-a", version=claimed.ownership_version,
        expected_hash=content_hash(claimed),
    ))
    changed = result.model_copy(update={"observations": []})
    with pytest.raises(StateConflict, match="original_probe_observation_removed"):
        await deps.central_state.transaction(lambda tx: tx.publish_result(
            str(claimed.task_id), changed, owner="worker-a", version=claimed.ownership_version,
            expected_hash=content_hash(saved),
        ))
    assert (await service.get_hypothesis("candidate-a")).status == "open"


@pytest.mark.parametrize("mode", ["missing_target", "missing_evidence", "bad_revision",
                                  "bad_source", "required_without_reason", "positive_no_review"])
async def test_bad_admission_has_no_partial_writes(tmp_path, mode):
    deps, service = await setup(tmp_path)
    update, proposed = candidate(), probe()
    if mode == "missing_target":
        update["hypothesis"]["target_node_ids"] = ["host:unknown"]
    elif mode == "missing_evidence":
        update["hypothesis"]["supporting_evidence_ids"] = ["missing"]
    elif mode == "bad_revision":
        proposed["hypothesis_revisions"] = {"candidate-a": 9}
    elif mode == "bad_source":
        proposed.update(source_task_id="missing", source_result_id="result-missing")
    elif mode == "required_without_reason":
        proposed["required"] = True
    else:
        update["hypothesis"]["status"] = "confirmed"
    with pytest.raises((StateConflict, ValidationError)):
        await admit(deps, service, "bad", updates=[update], tasks=[proposed])
    assert await deps.central_state.list_tasks() == []
    assert await service.get_hypothesis("candidate-a") is None


def test_old_serialized_contracts_remain_readable_and_identical():
    legacy = {"hypothesis_id": "old", "description": "Legacy question", "confidence": 0.1,
              "status": "old-custom-status", "metadata": {}}
    assert Hypothesis.model_validate(legacy).model_dump() == legacy
    old_task = task().model_dump(mode="json")
    assert "investigation_contract_version" not in old_task
    assert MissionTask.model_validate(old_task).model_dump(mode="json") == old_task
    old_result = {"result_id": "r", "outcome": "blocked", "summary": "Missing prerequisite",
                  "evidence_ids": [], "hypothesis_ids": [], "finding_node_ids": [],
                  "completion_receipt_id": None, "questions": []}
    assert InvestigationResult.model_validate(old_result).model_dump() == old_result


async def test_followup_cas_detects_new_result_with_the_same_id(tmp_path):
    deps, service = await setup(tmp_path)
    receipt = await admit(deps, service, "first", updates=[candidate()], tasks=[probe()])
    state = deps.central_state
    claimed = await state.claim_task(receipt["task_ids"][0], owner="worker-a", instance_id="i-a")
    partial = result_for(claimed, outcome="inconclusive", summary="Partial response",
                         evidence_ids=[], observations=["First observation"])
    source = await state.transaction(lambda tx: tx.publish_result(
        str(claimed.task_id), partial, owner="worker-a", version=claimed.ownership_version,
        expected_hash=content_hash(claimed),
    ))
    second = result_for(source, outcome="blocked", summary="Missing prerequisite",
                        evidence_ids=[], limitations=["No session available"])
    await state.transaction(lambda tx: tx.publish_result(
        str(claimed.task_id), second, owner="worker-a", version=claimed.ownership_version,
        expected_hash=content_hash(source),
    ))
    assert partial.result_id == second.result_id
    with pytest.raises(StateConflict, match="probe_related_result_changed"):
        await admit(deps, service, "stale-followup", tasks=[probe(
            "followup", source_task_id=str(claimed.task_id), source_result_id=partial.result_id,
        )], task_read={str(claimed.task_id): content_hash(source)})
    assert len(await state.list_tasks()) == 1
