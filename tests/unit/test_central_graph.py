"""Focused central graph checks with temporary SQLite and synthetic evidence."""

import asyncio
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor

import pytest

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    SensitiveValueError,
    attrs_of,
    claim_provenance_of,
    facts_of,
)
from redteam.attack_graph.service import (
    AttackGraphService,
    NodeTypeConflictError,
    UngroundedFindingError,
)
from redteam.attack_graph.stores.central import GraphTransaction
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationConflict,
    mutation_hash,
)
from redteam.core.contracts.events import EventEnvelope
from redteam.core.contracts.graph import Finding, GraphNode
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import GraphEdgeType, GraphNodeType, TaskStatus
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.definition import ContextProfile
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.context_projection import ContextProjectionCache, ContextProjector
from redteam.orchestration.knowledge import HypothesisService
from tests.unit.test_central_state import ENG, MISSION, NOW, RUN, original, owner, task, world


def node(identity="host:a", **properties):
    return GraphNode(id=identity, type=GraphNodeType.HOST, properties=properties,
                     created_at=NOW, updated_at=NOW)


def request(operation="add_node", *, mutation_id="mutation-a"):
    from redteam.core.contracts.graph import GraphEdge

    payload = {"node": node(facts={"result": "original"})} if operation == "add_node" else (
        {"edge": GraphEdge(id="related_to:host:a->host:b", type=GraphEdgeType.RELATED_TO,
                           source_id="host:a", target_id="host:b", properties={"a": 1},
                           created_at=NOW)} if operation == "add_edge" else
        {"identity": "host:a" if operation == "remove_node" else "related_to:host:a->host:b"}
    )
    return GraphMutation(run_id=RUN, mutation_id=mutation_id, operation=operation,
                         references=("graph", "node:host:a"), **payload)


def _write_process(path, writer):
    async def write():
        state = CentralStateStore(path, run_id=RUN, engagement_id=ENG)
        await state.setup()
        service = AttackGraphService(build_central_graph(state))
        for step in range(4):
            provenance = Provenance(step=step, source=writer, evidence_id=f"ev-{writer}-{step}",
                                    run_id=RUN, confidence=Confidence.MEASURED)
            await service.upsert_node(GraphNodeType.HOST, key="a",
                                      facts={"observations": [f"{writer}-{step}"]},
                                      attrs={writer: True}, provenance=provenance, step=step)
            await service.link(GraphEdgeType.RELATED_TO, "host:a", "host:b",
                               {writer: step}, provenance=provenance)
    asyncio.run(write())


async def test_process_writers_merge_latest_node_edge_and_provenance(tmp_path):
    state = await world(tmp_path)
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        futures = [pool.submit(_write_process, str(state.path), w) for w in ("one", "two")]
        await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
    service = AttackGraphService(build_central_graph(state))
    saved = await service.get_node("host:a")
    assert set(facts_of(saved.properties)["observations"]) == {
        f"{writer}-{step}" for writer in ("one", "two") for step in range(4)
    }
    assert attrs_of(saved.properties) == {"one": True, "two": True}
    assert {p["source"] for p in saved.properties["provenance"]} == {"one", "two"}
    edge = await service.get_edge("related_to:host:a->host:b")
    assert edge.properties["one"] == edge.properties["two"] == 3
    assert {p["source"] for p in edge.properties["provenance"]} == {"one", "two"}
    claims = claim_provenance_of(edge.properties)
    assert claims["facts:one"]["run_id"] == claims["facts:two"]["run_id"] == RUN


async def test_sql_targeted_reads_reach_service_view_and_context_consumer(tmp_path, monkeypatch):
    state = await world(tmp_path)
    service = AttackGraphService(build_central_graph(state))
    a = await service.upsert_node(GraphNodeType.HOST, key="A".lower(), attrs={"reachable": True})
    b = await service.upsert_node(GraphNodeType.SERVICE, key="a/http", labels=["web"])
    edge = await service.link(GraphEdgeType.HOSTS, a.id, b.id)
    await service.upsert_node(GraphNodeType.CREDENTIAL, key="unrelated")
    # A poisoned unrelated row proves that point, typed and neighborhood reads
    # do not deserialize a whole-graph mirror or scan every row in Python.
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE state_graph_nodes SET payload='invalid' WHERE node_type='credential'")

    def full_read(*args, **kwargs):
        raise AssertionError("whole graph read on a targeted path")
    monkeypatch.setattr(GraphTransaction, "list_nodes", full_read)
    monkeypatch.setattr(GraphTransaction, "list_edges", full_read)
    assert await service.get_node(a.id) == a
    assert await service.get_edge(edge.id) == edge
    assert await service.neighbors(a.id) == [b]
    assert await service.query(node_type="service", label="web") == [b]
    assert await service.query(node_type="host", attrs={"reachable": True, "missing": None}) == [a]
    with pytest.raises(TypeError, match="unsupported query"):
        await service.query(unsupported=True)
    nodes, edges = await service.subgraph_around(a.id)
    assert {n.id for n in nodes} == {a.id, b.id} and edges == [edge]
    assert await service.view().subgraph_around(a.id, radius=0) == ([a], [])
    assert await service.view().has_edge(a.id, b.id, GraphEdgeType.HOSTS)
    projector = ContextProjector(service, ContextProjectionCache())
    projection = await projector.project(
        ContextProfile(profile_id="local", neighbor_depth=1), step=1,
        target_node_ids=[a.id], fallback=GraphObservation(),
    )
    assert set(sum(projection.node_ids_by_type.values(), [])) == {a.id, b.id}
    assert await HypothesisService(service).relations_for("missing") == []

    # This service was already used before a separate store/connection wrote.
    other = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    await AttackGraphService(build_central_graph(other)).upsert_node(
        GraphNodeType.HOST, key="a", facts={"status_code": 200},
    )
    assert facts_of((await service.get_node(a.id)).properties)["status_code"] == 200
    later = await projector.project(
        ContextProfile(profile_id="local", neighbor_depth=1), step=1,
        target_node_ids=[a.id], fallback=GraphObservation(),
    )
    assert later != projection  # Same step/revision must not serve stale central data.


@pytest.mark.parametrize("failure_table", [
    "state_evidence_refs", "state_graph_nodes", "state_graph_mutations", "state_outbox",
])
async def test_task_graph_receipt_refs_and_outbox_share_rollback(tmp_path, failure_table):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    checked = await original(evidence, "receipt-a")
    result = InvestigationResult(
        result_id="result-a", outcome="refuted", summary="No issue reproduced",
        evidence_ids=["receipt-a"], questions=[{
            "question_id": "q1", "question": "Check another principal",
            "required_for_goal": True, "reason": "Authorization remains in scope",
            "evidence_ids": ["receipt-a"],
        }],
    )
    graph = build_central_graph(state)
    prepared = await graph.prepare_graph_mutation(request())
    event = EventEnvelope(event_id="event-a", event_type="synthetic.result", run_id=RUN,
                          engagement_id=ENG, source="synthetic", timestamp=NOW, payload={})
    with sqlite3.connect(state.path) as db:
        db.execute(f"CREATE TRIGGER synthetic_failure BEFORE INSERT ON {failure_table} "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
    def settle(tx):
        tx.finish_task("task-a", result, owner="worker-a", version=1, checked=checked)
        receipt = tx.graph.apply_graph_mutation(prepared)
        tx.queue(event)
        return receipt
    with pytest.raises(sqlite3.IntegrityError, match="synthetic storage failure"):
        await state.transaction(settle)
    assert (await state.get_task("task-a")).result is None
    assert await graph.get_node("host:a") is None
    assert await graph.read_graph_mutation_receipt(RUN, "mutation-a") is None
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM state_evidence_refs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM state_outbox").fetchone()[0] == 0
        db.execute("DROP TRIGGER synthetic_failure")
    receipt = await state.transaction(settle)
    assert await state.transaction(settle) == receipt
    completed = await state.get_task("task-a")
    assert completed.status is TaskStatus.COMPLETED and completed.result == result
    assert (await state.inspect_evidence(evidence))["external_only"] == []
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM state_outbox").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["add_node", "add_edge", "remove_node", "remove_edge"])
async def test_original_receipt_survives_lost_ack_restart_and_never_replays(tmp_path, operation):
    state = await world(tmp_path)
    graph = build_central_graph(state)
    service = AttackGraphService(graph)
    await service.upsert_node(GraphNodeType.HOST, key="a")
    await service.upsert_node(GraphNodeType.HOST, key="b")
    await service.link(GraphEdgeType.RELATED_TO, "host:a", "host:b")
    prepared = await graph.prepare_graph_mutation(request(operation))
    # Discard the response exactly at the commit/ack boundary.
    await graph.apply_graph_mutation(prepared)
    reopened = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    await reopened.setup()
    recovered = build_central_graph(reopened)
    receipt = await recovered.read_graph_mutation_receipt(RUN, "mutation-a")
    assert receipt.request_hash == mutation_hash(prepared)
    await AttackGraphService(recovered).upsert_node(GraphNodeType.HOST, key="a",
                                                   facts={"result": "later"})
    await AttackGraphService(recovered).link(GraphEdgeType.RELATED_TO, "host:a", "host:b",
                                            {"a": 2})
    assert await recovered.apply_graph_mutation(prepared) == receipt
    assert facts_of((await recovered.get_node("host:a")).properties)["result"] == "later"
    assert (await recovered.get_edge("related_to:host:a->host:b")).properties["a"] == 2
    with pytest.raises(GraphMutationConflict, match="receipt_conflict"):
        await recovered.apply_graph_mutation(prepared.model_copy(update={"references": ("graph",)}))
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE state_graph_mutations SET receipt=json_set(receipt,'$.run_id','wrong')")
    with pytest.raises(GraphMutationConflict, match="index_conflict"):
        await recovered.read_graph_mutation_receipt(RUN, "mutation-a")


async def test_engagement_knowledge_and_original_run_receipts_are_scoped(tmp_path):
    state = await world(tmp_path)
    graph = build_central_graph(state)
    prepared = await graph.prepare_graph_mutation(request())
    await graph.apply_graph_mutation(prepared)
    same_eng = await world(tmp_path, run="run-two", mission_id="mission-two")
    same_graph = build_central_graph(same_eng)
    assert await same_graph.get_node("host:a") is not None
    assert await same_graph.read_graph_mutation_receipt("run-two", "mutation-a") is None
    with pytest.raises(GraphMutationConflict, match="scope_conflict"):
        await same_graph.apply_graph_mutation(prepared)
    other = await world(
        tmp_path, run="other-run", engagement="other-eng", mission_id="other-mission",
    )
    other_graph = build_central_graph(other)
    assert await other_graph.get_node("host:a") is None
    assert await other_graph.list_nodes() == []
    with pytest.raises(GraphMutationConflict, match="namespace_conflict"):
        await other_graph.apply_graph_mutation(prepared)


async def test_merge_retraction_grounding_secret_guard_and_snapshot_contract(tmp_path):
    state = await world(tmp_path)
    service = AttackGraphService(build_central_graph(state))
    measured = Provenance(step=1, source="tool:synthetic", run_id=RUN,
                          evidence_id="ev-original", confidence=Confidence.MEASURED)
    saved = await service.upsert_node(GraphNodeType.HOST, key="a", attrs={"reachable": True},
                                      facts={"observed": "value"}, provenance=measured, step=1)
    repeated = await service.upsert_node(
        GraphNodeType.HOST, key="a", attrs={"reachable": True},
        facts={"observed": "value"}, provenance=measured, step=1,
    )
    assert repeated == saved
    with pytest.raises(NodeTypeConflictError):
        await service.upsert_node(GraphNodeType.SERVICE, node_id=saved.id)
    assert await service.retract(saved.id, "reachable", step=4, reason="Observation expired")
    stale = await service.upsert_node(
        GraphNodeType.HOST, key="a", attrs={"reachable": True}, step=1,
    )
    assert "reachable" not in attrs_of(stale.properties)
    assert stale.properties["retractions"][0]["reason"] == "Observation expired"
    assert claim_provenance_of(stale.properties)["facts:observed"]["evidence_id"] == "ev-original"
    with pytest.raises(SensitiveValueError):
        await service.upsert_node(GraphNodeType.HOST, key="a", facts={"password": "synthetic"})
    with pytest.raises(UngroundedFindingError):
        await service.record_finding(Finding(finding_id="f1", title="x", verified=True))
    unsafe = request().model_copy(update={"node": node(facts={"password": "synthetic"})})
    graph = build_central_graph(state)
    with pytest.raises(SensitiveValueError):
        await graph.apply_graph_mutation(await graph.prepare_graph_mutation(unsafe))
    from redteam.attack_graph.snapshot import from_records, to_records

    records = to_records(await service.list_nodes(), await service.list_edges(),
                         metadata={"run_id": RUN})
    nodes, edges, metadata = from_records(records)
    assert metadata == {"run_id": RUN}
    assert to_records(nodes, edges, metadata=metadata) == records
    await service.upsert_node(GraphNodeType.HOST, key="a", facts={"extra": "later"})
    # Serialization is a detached storage representation, never a live restore API.
    assert "extra" not in nodes[0].properties["facts"]
    assert "extra" in (await service.get_node(saved.id)).properties["facts"]


async def test_schema_one_upgrades_preserving_state_and_async_updates_are_refused(tmp_path):
    state = await world(tmp_path)
    await state.register_task("registration", task())
    before = await state.get_task("task-a")
    with sqlite3.connect(state.path) as db:
        for table in ("state_migrated_runs", "state_migration_records", "state_migrations",
                      "state_graph_mutations", "state_graph_edges", "state_graph_nodes"):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE state_schema SET version=1")
    await state.setup()
    assert await state.get_task("task-a") == before
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT version FROM state_schema").fetchone()[0] == 3
        assert db.execute("SELECT mission_id FROM state_runs").fetchone()[0] == MISSION
    async def invalid_update(current):
        return node()
    with pytest.raises(TypeError, match="synchronous"):
        await state.transaction(lambda tx: tx.graph.mutate_node(
            f"{ENG}\x1fhost:a", invalid_update,
        ))


async def test_legacy_namespace_edge_lookup_keeps_properties_on_service_relink():
    from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
    from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
    from redteam.core.contracts.graph import GraphEdge

    inner = InMemoryAttackGraphStore()
    scoped = EngagementScopedAttackGraphStore(inner, engagement_id=ENG)
    await inner.add_edge(GraphEdge(
        id="related_to:host:a->host:b", type=GraphEdgeType.RELATED_TO,
        source_id=f"{ENG}\x1fhost:a", target_id=f"{ENG}\x1fhost:b",
        properties={"original": True}, created_at=NOW,
    ))
    linked = await AttackGraphService(scoped).link(
        GraphEdgeType.RELATED_TO, "host:a", "host:b", {"new": True},
    )
    assert linked.properties == {"original": True, "new": True}
    assert await scoped.get_edge(linked.id) == linked
