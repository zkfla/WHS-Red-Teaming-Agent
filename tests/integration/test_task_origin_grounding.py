"""Task-origin grounding fallback: measured host-endpoint relations only.

Bounded proof for the semantic_completion fallback that accepts a
non-member evidence target only through an EXISTING measured, same-run,
same-record relation edge (analysis.grounding_edge_for). No reachability
relaxation: unrelated / forged / stale / foreign-run relations stay
rejected, and the final SQL commit rechecks the exact edge proof.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence
from redteam.attack_graph.predicates import Provenance as GraphProvenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.central import GRAPH_SCHEMA, GraphTransaction
from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.core.contracts.completion import criterion_id
from redteam.core.contracts.target import UrlTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import AgentPhase, GraphEdgeType, TaskStatus
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import settle_central
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.local_tools import LocalToolContext
from tests.integration.test_central_completion import OriginalProtocolDouble, selection
from tests.integration.test_central_development import TARGET, call, discover, register, turn

URL = "http://unit.local/"
URL_TOOL = "web.fetch"
ENDPOINT = str(node_id_for_target(UrlTarget(url=URL)))
OTHER_ENDPOINT = str(node_id_for_target(UrlTarget(url="http://unit.local/other")))


def url_gateway(*, responses=4):
    tool = ToolDescriptor.model_validate(
        {
            "tool_id": URL_TOOL,
            "server_id": "development",
            "tool_name": "fetch",
            "capability_tags": ["web"],
            "risk_level": "low",
            "health_status": "healthy",
            "target_types": ["url"],
            "argument_bindings": {"url": "url"},
            "resource_bindings": [],
            "input_schema": {
                "type": "object",
                "required": ["url", "account"],
                "properties": {
                    "url": {"type": "string"},
                    "account": {"type": "string"},
                },
            },
        }
    )
    return DevelopmentToolGateway(
        [tool],
        [
            DevelopmentResponse(output={"status": 200, "url": URL}, metadata={})
            for _ in range(responses)
        ],
    )


def url_fetch(identity):
    return call(identity, URL_TOOL, {"url": URL, "account": "a"})


async def start_url(session, llm):
    await register(session, "registration-grounding")
    tasks = await session.deps.central_state.list_tasks()
    for task in tasks:
        llm.native_responses_by_task[str(task.task_id)] = [
            discover(URL_TOOL),
            turn(url_fetch(f"call-{task.task_id}")),
            NativeCompletion(text="Endpoint response inspected", total_tokens=1),
        ]
    workers = await ready_workers(session.deps, session.runtime)
    runtimes = []
    async for result in session.driver.drive(workers):
        assert result.kind == "completed", result.error
        runtimes.append(result.runtime)
    return runtimes


async def endpoint_originals(session, runtime):
    records = await session.deps.evidence_store.list_for_run(runtime.run_id)
    return [
        str(record.evidence_id)
        for record in records
        if isinstance(record.metadata.get("tool_result"), dict)
        and record.metadata.get("mission_task_id") == str(runtime.active_task_id)
        and str(record.metadata.get("target_node_id") or "") == ENDPOINT
    ]


async def link_host_endpoint(session, runtime, evidence_id, *, confidence=None, run_id=None,
                             evidence=None, endpoint=ENDPOINT):
    """Production-shaped HOSTS(host -> endpoint) relation edge.

    Mirrors map_web_http_request: the executed request measured this
    resource on its source target, recorded with the request's own
    evidence provenance.
    """
    graph = AttackGraphService(session.deps.graph_store)
    relation = {
        "type": GraphEdgeType.HOSTS.value,
        "source_node_id": TARGET,
        "target_node_id": endpoint,
    }
    return await graph.link(
        GraphEdgeType.HOSTS,
        GraphNodeId(TARGET),
        GraphNodeId(endpoint),
        properties={"relation": relation},
        provenance=GraphProvenance(
            run_id=run_id or str(runtime.run_id),
            evidence_id=evidence or evidence_id,
            source="tool:fetch",
            confidence=confidence or Confidence.MEASURED,
        ),
    )


async def arm_endpoint_review(llm, session, runtime, originals):
    task = await session.deps.central_state.get_task(str(runtime.active_task_id))
    llm._canned_structured = {
        "accepted": True,
        "reason": "Endpoint response assessed",
        "claims": [{
            "kind": "semantic",
            "description": "Endpoint response assessed",
            "evidence_ids": originals,
        }],
        "criterion_results": [{
            "criterion_id": criterion_id(condition),
            "met": True,
            "evidence_ids": originals,
            "reason": "Endpoint assessed",
        } for condition in task.completion_criteria],
    }
    proposal = CompletionProposal(
        outcome="success", reason="Endpoint assessed", evidence_ids=originals,
    )
    context = LocalToolContext(
        session.deps, runtime.model_copy(update={"phase": AgentPhase.OBSERVE}),
    )
    return proposal, context


async def settle_endpoint(session, llm, runtime, originals):
    proposal, context = await arm_endpoint_review(llm, session, runtime, originals)
    return await settle_central("task", proposal, context)


async def task_status(session, runtime):
    return await session.deps.central_state.get_task(str(runtime.active_task_id))


async def test_endpoint_under_related_host_settles(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, tools=OriginalProtocolDouble(url_gateway()),
                           llm=llm) as (session, llm):
        runtime, = await start_url(session, llm)
        originals = await endpoint_originals(session, runtime)
        assert len(originals) == 1
        await link_host_endpoint(session, runtime, originals[0])
        result = await settle_endpoint(session, llm, runtime, originals)
        assert result.ok, result.error
        task = await task_status(session, runtime)
        assert task.status is TaskStatus.COMPLETED
        assert task.result.completion_receipt_id


async def test_endpoint_without_relation_rejected(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, tools=OriginalProtocolDouble(url_gateway()),
                           llm=llm) as (session, llm):
        runtime, = await start_url(session, llm)
        originals = await endpoint_originals(session, runtime)
        assert len(originals) == 1
        result = await settle_endpoint(session, llm, runtime, originals)
        assert not result.ok
        assert originals[0] in (result.error or "")
        assert "targets an object outside this task" in (result.error or "")
        task = await task_status(session, runtime)
        assert task.status is TaskStatus.RUNNING


@pytest.mark.parametrize("case", ["forged", "foreign_run", "stale"])
async def test_unproven_relation_rejected(tmp_path, case):
    llm = FakeLLMClient()
    async with selection(tmp_path, tools=OriginalProtocolDouble(url_gateway()),
                           llm=llm) as (session, llm):
        runtime, = await start_url(session, llm)
        originals = await endpoint_originals(session, runtime)
        assert len(originals) == 1
        if case == "forged":
            await link_host_endpoint(session, runtime, originals[0],
                                     evidence="other-evidence")
        elif case == "foreign_run":
            await link_host_endpoint(session, runtime, originals[0],
                                     run_id="run-elsewhere")
        else:
            await link_host_endpoint(session, runtime, originals[0],
                                     confidence=Confidence.INFERRED)
        result = await settle_endpoint(session, llm, runtime, originals)
        assert not result.ok
        assert "targets an object outside this task" in (result.error or "")
        task = await task_status(session, runtime)
        assert task.status is TaskStatus.RUNNING


async def test_grounding_edge_removed_during_review_rejected(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, tools=OriginalProtocolDouble(url_gateway()),
                           llm=llm) as (session, llm):
        runtime, = await start_url(session, llm)
        originals = await endpoint_originals(session, runtime)
        assert len(originals) == 1
        edge = await link_host_endpoint(session, runtime, originals[0])
        proposal, context = await arm_endpoint_review(llm, session, runtime, originals)
        requests = len(llm.requests)
        llm.request_release.clear()
        pending = asyncio.create_task(settle_central("task", proposal, context))
        try:
            await asyncio.wait_for(llm.wait_for_requests(requests + 1), 5)
            removed = await session.deps.graph_store.remove_edge(GraphEdgeId(str(edge.id)))
            assert removed
        finally:
            llm.request_release.set()
        result = await asyncio.wait_for(pending, 10)
        assert not result.ok
        task = await task_status(session, runtime)
        assert task.status is TaskStatus.RUNNING


@pytest.mark.parametrize("related", [True, False])
async def test_final_commit_rechecks_grounding_edge(tmp_path, monkeypatch, related):
    from redteam.orchestration import central_results

    llm = FakeLLMClient()
    async with selection(tmp_path, tools=OriginalProtocolDouble(url_gateway()),
                           llm=llm) as (session, llm):
        runtime, = await start_url(session, llm)
        originals = await endpoint_originals(session, runtime)
        assert len(originals) == 1
        edge = await link_host_endpoint(session, runtime, originals[0])
        decoy = await link_host_endpoint(session, runtime, originals[0],
                                         endpoint=OTHER_ENDPOINT)
        original_commit = central_results.commit_result

        async def race(*args, **kwargs):
            if kwargs.get("edge_proof"):
                await session.deps.graph_store.remove_edge(
                    GraphEdgeId(str(edge.id) if related else str(decoy.id)))
            return await original_commit(*args, **kwargs)

        monkeypatch.setattr(central_results, "commit_result", race)
        if related:
            with pytest.raises(StateConflict, match="grounding_edge"):
                await settle_endpoint(session, llm, runtime, originals)
        else:
            result = await settle_endpoint(session, llm, runtime, originals)
            assert result.ok, result.error
        task = await task_status(session, runtime)
        assert (task.status is TaskStatus.COMPLETED) == (not related)


def test_get_local_edge_returns_public_shape(tmp_path):
    from redteam.core.contracts.graph import GraphEdge

    db = sqlite3.connect(tmp_path / "graph.sqlite3")
    for statement in GRAPH_SCHEMA:
        db.execute(statement)
    prefix = "eng\x1f"
    tx = GraphTransaction(db, namespace="eng", run_id="run")
    relation = {
        "type": GraphEdgeType.HOSTS.value,
        "source_node_id": TARGET,
        "target_node_id": ENDPOINT,
    }
    stored = GraphEdge(
        id=GraphEdgeId(prefix + "edge:grounding:1"),
        type=GraphEdgeType.HOSTS,
        source_id=GraphNodeId(prefix + TARGET),
        target_id=GraphNodeId(prefix + ENDPOINT),
        properties={"relation": relation},
        created_at=datetime.now(timezone.utc),
    )
    written = tx.mutate_edge(GraphEdgeId(prefix + "edge:grounding:1"), lambda _old: stored)
    assert written is not None
    got = tx.get_local_edge(GraphEdgeId("edge:grounding:1"))
    assert got is not None
    assert str(got.id) == "edge:grounding:1"
    assert str(got.source_id) == TARGET
    assert str(got.target_id) == ENDPOINT
    assert tx.get_local_edge(GraphEdgeId("edge:missing")) is None
