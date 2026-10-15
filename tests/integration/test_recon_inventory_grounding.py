"""Recon inventory discovery edges feed the shared grounding reader.

The recon HTML and OpenAPI mappers record that a source target's document held
a same-origin reference to an endpoint. That edge must use the canonical
relation snapshot that analysis.grounding_edge_for consumes, so the exact
original can ground the endpoint in the same run. The edge proves only that the
original contained the reference: no reachability, content, status, or security
meaning is granted here. Historical rows are not bulk-rewritten; a fresh
observation can update the same edge through the existing merge contract.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import endpoint_node_id, node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.analysis import claim_target_is_grounded, grounding_edge_for
from redteam.orchestration.observations import GraphMutation, default_result_mapper_registry
from tests.integration import _engine_llm_harness as h

TARGET = HostTarget(host="bank.local")
SOURCE = node_id_for_target(TARGET)
ENDPOINT = endpoint_node_id("GET", "/accounts/123", host="bank.local")
OTHER_ENDPOINT = endpoint_node_id("GET", "/reports/summary", host="bank.local")
SHA256 = "a" * 64
RUN = "run-recon"
EVIDENCE = "ev-recon-original"


def _html_output() -> dict:
    return {
        "target_ref": "bank",
        "references": [{
            "kind": "a",
            "path": "/accounts/123",
            "method": "GET",
            "query_omitted": True,
            "fragment_omitted": False,
            "path_allowlisted": True,
        }],
        "reference_limit": 200,
        "references_truncated": False,
        "omitted_references": 0,
        "base_element_present": False,
        "decoding_replacements": False,
        "references_followed": False,
        "javascript_executed": False,
        "source_untrusted": True,
        "verdict": "observation_only",
        "source_sha256": SHA256,
        "request_count": 1,
    }


def _openapi_output() -> dict:
    return {
        "target_ref": "bank",
        "operations": [{
            "path": "/accounts/123",
            "method": "GET",
            "declares_security": False,
            "deprecated": False,
        }],
        "limit": 200,
        "may_be_truncated": False,
        "external_refs_followed": False,
        "source_untrusted": True,
        "source_sha256": SHA256,
    }


def _map(tool_id: str, output: dict) -> GraphMutation:
    """Run the production registry mapper on one successful tool output."""
    now = datetime.now(timezone.utc)
    result = ToolResult(
        invocation_id=InvocationId("inv-recon"),
        action_id=ActionId("act-recon"),
        tool_id=ToolId(tool_id),
        success=True,
        output=output,
        started_at=now,
        finished_at=now,
    )
    return default_result_mapper_registry().map_result(
        tool_id=ToolId(tool_id),
        target=TARGET,
        arguments={"path": "/index.html", "target_ref": "bank"},
        result=result,
    )


async def _apply(svc, mutation, *, run_id=RUN, evidence_id=EVIDENCE,
                 confidence=Confidence.MEASURED):
    """Write a mapper batch the way UPDATE does: nodes, then edges, one provenance."""
    provenance = Provenance(
        step=1, source="tool:recon", run_id=run_id,
        evidence_id=evidence_id, confidence=confidence,
    )
    for node in mutation.nodes:
        await svc.upsert_node(
            node.node_type, node_id=node.node_id, attrs=node.attrs,
            facts=node.facts, labels=node.labels, provenance=provenance, step=1,
        )
    for edge in mutation.edges:
        await svc.link(
            edge.edge_type, edge.source_id, edge.target_id, edge.properties,
            provenance=provenance,
        )


def _record():
    record = h.make_tool_evidence(
        evidence_id=EVIDENCE,
        run_id=RUN,
        engagement_id="eng-recon",
        action_id="act-recon",
        invocation_id="inv-recon",
        path_id="path-1",
        path_version=1,
        step_id="step-1",
        target_node_id=str(SOURCE),
    )
    proposal, _ = h.make_proposal_result(
        run_id=RUN,
        engagement_id="eng-recon",
        action_id="act-recon",
        invocation_id="inv-recon",
        host="bank.local",
    )
    record.metadata["proposal"] = proposal.model_dump(mode="json")
    return record


def _svc():
    return AttackGraphService(InMemoryAttackGraphStore())


@pytest.mark.parametrize(
    "tool_id, output",
    [
        ("recon.html_inventory", _html_output()),
        ("recon.openapi_inventory", _openapi_output()),
    ],
)
async def test_original_inventory_edge_grounds_its_endpoint(tool_id, output):
    svc = _svc()
    await _apply(svc, _map(tool_id, output))
    edge = await grounding_edge_for(svc, _record(), str(ENDPOINT))
    assert edge is not None
    assert edge.type is GraphEdgeType.OBSERVED
    assert edge.properties["relation"] == {
        "type": GraphEdgeType.OBSERVED.value,
        "source_node_id": str(SOURCE),
        "target_node_id": str(ENDPOINT),
    }
    assert await claim_target_is_grounded(svc, _record(), str(ENDPOINT))


@pytest.mark.parametrize("case", ["foreign_run", "forged_evidence", "inferred"])
async def test_unproven_inventory_edge_rejected(case):
    svc = _svc()
    mapped = _map("recon.html_inventory", _html_output())
    if case == "foreign_run":
        await _apply(svc, mapped, run_id="run-elsewhere")
    elif case == "forged_evidence":
        await _apply(svc, mapped, evidence_id="other-ev")
    else:
        await _apply(svc, mapped, confidence=Confidence.INFERRED)
    assert await grounding_edge_for(svc, _record(), str(ENDPOINT)) is None


async def test_mutated_endpoint_and_relation_rejected():
    svc = _svc()
    mutation = _map("recon.html_inventory", _html_output())
    await _apply(svc, mutation)
    assert await grounding_edge_for(svc, _record(), str(OTHER_ENDPOINT)) is None
    edge = mutation.edges[0]
    tampered = replace(edge, properties={
        **edge.properties,
        "relation": {
            "type": GraphEdgeType.OBSERVED.value,
            "source_node_id": str(SOURCE),
            "target_node_id": str(OTHER_ENDPOINT),
        },
    })
    await _apply(svc, GraphMutation(edges=[tampered]))
    assert await grounding_edge_for(svc, _record(), str(ENDPOINT)) is None


async def test_legacy_string_relation_row_stays_ungrounded():
    svc = _svc()
    edge = _map("recon.html_inventory", _html_output()).edges[0]
    legacy = GraphMutation(edges=[replace(edge, properties={"relation": "surface_discovery"})])
    await _apply(svc, legacy)
    assert await grounding_edge_for(svc, _record(), str(ENDPOINT)) is None

