"""context.query graph roots come from the worker's own Task targets.

Real SQLite central state and graph through the native handler. No model or
target call is made. Only the Task's related targets and its forward ancestry
feed the bounded neighborhood; a model query cannot add roots.
"""

from __future__ import annotations

from types import SimpleNamespace

from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.native_local_tools import context_query_handler
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.unit.test_central_state import BINDING
from tests.unit.test_hypothesis_probes import probe, setup


def _candidate(targets):
    return {"expected_revision": None, "hypothesis": {
        "contract_version": 2, "hypothesis_id": "candidate-a",
        "description": "Access may be unguarded", "target_node_ids": list(targets),
        "reason": "Verify the observed response", "status": "open",
        "supporting_evidence_ids": [],
    }}


def _context(deps, task_id):
    runtime = SimpleNamespace(active_task_id=task_id)
    return LocalToolContext(deps=deps, runtime=runtime, purpose="test")


async def _admit(deps, service, request_id, *, updates=(), tasks=()):
    """Probe admission as in test_hypothesis_probes.admit, with both targets known."""
    graph_read = {}
    for identity in ("host:a", "host:c", "hypothesis:candidate-a"):
        node = await service.get_node(identity)
        if node is not None:
            graph_read[identity] = content_hash(node)
    proposal = HypothesisProbeProposal.model_validate(
        {"updates": list(updates), "tasks": list(tasks)},
    )
    return await register_probe_tasks(
        deps, proposal_id=request_id, proposal=proposal, expected_revision=1,
        expected_binding_hash=BINDING, graph_read=graph_read,
        known_targets={"host:a", "host:c"}, known_principals=set(), max_tasks=4, task_read={},
    )


async def _world(tmp_path):
    deps, service = await setup(tmp_path)
    for key in ("c", "d", "z"):
        await service.upsert_node(GraphNodeType.HOST, key=key, facts={"reachable": True})
    await service.link(GraphEdgeType.RELATED_TO, "host:a", "host:c")
    await service.link(GraphEdgeType.RELATED_TO, "host:a", "host:d")
    await _admit(deps, service, "seed", updates=[_candidate(["host:a", "host:c"])])
    receipt = await _admit(deps, service, "p1", tasks=[probe("probe-a")])
    return deps, receipt["task_ids"][0]


async def test_worker_reads_its_own_target_and_edges_between_its_targets(tmp_path):
    deps, task_id = await _world(tmp_path)

    node = await context_query_handler(
        {"kind": "node", "mode": "read", "refs": [{"kind": "node", "identity": "host:a"}]},
        _context(deps, task_id),
    )
    assert node.ok is True
    assert node.payload["records"][0]["identity"] == "host:a"

    edges = await context_query_handler(
        {"kind": "edge", "mode": "list"}, _context(deps, task_id),
    )
    assert edges.ok is True
    assert edges.payload["count"] == 1
    edge = edges.payload["records"][0]["data"]
    assert {edge["source_id"], edge["target_id"]} == {"host:a", "host:c"}


async def test_unrelated_and_neighbor_targets_stay_outside_the_worker_scope(tmp_path):
    deps, task_id = await _world(tmp_path)

    # host:z is never captured. host:d is captured by the one-hop neighborhood but is
    # neither a Task target nor a candidate target, so it is outside this worker's scope.
    unrelated = await context_query_handler(
        {"kind": "node", "mode": "read", "refs": [{"kind": "node", "identity": "host:z"}]},
        _context(deps, task_id),
    )
    neighbor = await context_query_handler(
        {"kind": "node", "mode": "read", "refs": [{"kind": "node", "identity": "host:d"}]},
        _context(deps, task_id),
    )
    assert unrelated.ok is False
    assert unrelated.payload["code"] == "context_reference_not_found"
    assert neighbor.ok is False
    assert neighbor.payload["code"] == "context_reference_outside_read_scope"


async def test_model_query_cannot_widen_the_captured_roots(tmp_path):
    deps, task_id = await _world(tmp_path)

    widened = await context_query_handler(
        {"kind": "edge", "mode": "list", "target_node_ids": ["host:d"]},
        _context(deps, task_id),
    )
    assert widened.ok is False
    assert widened.payload["code"] == "context_reference_outside_read_scope"

    # The rejected query changes nothing: the worker still sees only its own edge.
    again = await context_query_handler(
        {"kind": "edge", "mode": "list"}, _context(deps, task_id),
    )
    assert again.ok is True
    assert again.payload["count"] == 1
