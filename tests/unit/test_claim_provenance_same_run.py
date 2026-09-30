"""Same-run claim provenance projection (new, no legacy fixtures)."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    build_properties,
    claim_provenance_of,
    merge_properties,
)
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType, GraphNodeType
from redteam.orchestration.knowledge import project_knowledge

RUN = "run-current"
OTHER_RUN = "run-other"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Graph:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    async def list_nodes(self):
        return list(self._nodes)

    async def list_edges(self):
        return []

    async def list_hypotheses(self, status=None):
        return []

    async def get_hypothesis(self, hid):
        return None

    async def get_node(self, nid):
        return None

    async def list_findings(self):
        return []


class _Evidence:
    def __init__(self, records):
        self._records = list(records)

    async def list_for_run(self, run_id):
        return [r for r in self._records if str(r.run_id) == str(run_id)]


def _tool_result(eid, output):
    return ToolResult(
        invocation_id="inv-" + str(eid),
        action_id="act-" + str(eid),
        tool_id="tool:probe",
        success=True,
        output=dict(output),
        started_at=NOW,
        finished_at=NOW,
        metadata={},
    )


def _record(eid, run_id, output=None):
    result = _tool_result(eid, {"seen": str(eid)} if output is None else output)
    return EvidenceRecord(
        evidence_id=eid,
        run_id=run_id,
        engagement_id="eng-1",
        source_type=EvidenceSourceType.TOOL,
        source_id="tool:probe",
        content_type="json",
        digest="d-" + str(eid),
        created_at=NOW,
        metadata={"tool_result": result.model_dump(mode="json")},
    )


def _node(nid, props):
    return GraphNode(
        id=nid,
        type=GraphNodeType.HOST,
        labels=[],
        properties=props,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_union_keeps_only_measured_elements():
    existing = build_properties(
        facts={"routes": ["/unverified"]},
        provenance=Provenance(
            step=0, source="engagement:scope", run_id=RUN, confidence=Confidence.SERVER_DECLARED
        ),
        step=0,
    )
    incoming = build_properties(
        facts={"routes": ["/real"]},
        provenance=Provenance(
            step=1,
            source="tool:probe",
            evidence_id="ev-real",
            run_id=RUN,
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    merged = merge_properties(existing, incoming)
    assert sorted(merged["facts"]["routes"]) == ["/real", "/unverified"]
    graph = _Graph([_node("host:web-1", merged)])
    ev = _Evidence([_record("ev-real", RUN, {"routes": ["/real"]})])
    proj = await project_knowledge(graph, ev, RUN, step=1)
    assert len(proj["facts"]) == 1
    item = proj["facts"][0]
    assert item["observed_facts"]["routes"] == ["/real"]
    assert "/unverified" not in item["observed_facts"]["routes"]
    assert "evidence:ev-real" in item["fact_refs"]


async def test_legacy_inferred_excluded_other_run_historical():
    legacy_props = {
        "attrs": {},
        "facts": {"legacy_flag": "x"},
        "provenance": [],
        "claim_provenance": {},
        "first_seen_step": 0,
        "last_seen_step": 0,
    }
    inferred = build_properties(
        facts={"guess": "y"},
        provenance=Provenance(
            step=0, source="llm:guess", run_id=RUN, confidence=Confidence.INFERRED
        ),
        step=0,
    )
    other = build_properties(
        facts={"other_val": "z"},
        provenance=Provenance(
            step=0,
            source="tool:probe",
            evidence_id="ev-other",
            run_id=OTHER_RUN,
            confidence=Confidence.MEASURED,
        ),
        step=0,
    )
    graph = _Graph(
        [
            _node("host:legacy-1", legacy_props),
            _node("host:inferred-1", inferred),
            _node("host:other-1", other),
        ]
    )
    ev = _Evidence([_record("ev-other", OTHER_RUN, {"other_val": "z"})])
    proj = await project_knowledge(graph, ev, RUN, step=0)
    assert proj["facts"] == []
    reasons = {(e["node_id"], e["attr"]): e["reason"] for e in proj["excluded_inferred_attrs"]}
    assert reasons[("host:legacy-1", "legacy_flag")] == "legacy_no_claim"
    assert reasons[("host:inferred-1", "guess")] == "no_measured_provenance"
    hist = proj["historical"]["facts"]
    assert any(h["node_id"] == "host:other-1" and h["run"] == OTHER_RUN for h in hist)
    assert proj["fact_refs"] == []


async def test_sticky_source_when_value_unchanged():
    existing = build_properties(
        facts={"status_code": 200},
        provenance=Provenance(
            step=0,
            source="first-write",
            evidence_id="ev-1",
            run_id=RUN,
            confidence=Confidence.MEASURED,
        ),
        step=0,
    )
    incoming = build_properties(
        facts={"status_code": 200},
        provenance=Provenance(
            step=1,
            source="second-write",
            evidence_id="ev-2",
            run_id=RUN,
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    merged = merge_properties(existing, incoming)
    claims = claim_provenance_of(merged)
    assert claims["facts:status_code"]["source"] == "first-write"
    assert claims["facts:status_code"]["evidence_id"] == "ev-1"


async def test_declared_config_never_becomes_fact_even_when_measured():
    props = build_properties(
        facts={
            "auth_session_config": {"sessions": [{"session_ref": "s-1"}]},
            "display_name": "web",
        },
        provenance=Provenance(
            step=1,
            source="tool:probe",
            evidence_id="ev-cfg",
            run_id=RUN,
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    graph = _Graph([_node("host:cfg-1", props)])
    ev = _Evidence([_record("ev-cfg", RUN, {"sessions": ["s-1"]})])
    proj = await project_knowledge(graph, ev, RUN, step=1)
    assert proj["facts"] == []
    reasons = {(e["node_id"], e["attr"]): e["reason"] for e in proj["excluded_inferred_attrs"]}
    assert reasons[("host:cfg-1", "auth_session_config")] == "declared_never_fact"
    assert reasons[("host:cfg-1", "display_name")] == "declared_never_fact"


async def test_measured_fact_refs_only():
    good = build_properties(
        facts={"good_val": "keep"},
        provenance=Provenance(
            step=1,
            source="tool:probe",
            evidence_id="ev-good",
            run_id=RUN,
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    bad = build_properties(
        facts={"bad_guess": "drop"},
        provenance=Provenance(
            step=1, source="llm:guess", run_id=RUN, confidence=Confidence.INFERRED
        ),
        step=1,
    )
    graph = _Graph([_node("host:good-1", good), _node("host:bad-1", bad)])
    ev = _Evidence([_record("ev-good", RUN, {"good_val": "keep"})])
    proj = await project_knowledge(graph, ev, RUN, step=1)
    assert len(proj["facts"]) == 1
    assert proj["facts"][0]["node_id"] == "host:good-1"
    joined = " ".join(proj["fact_refs"])
    assert "good_val" in joined
    assert "bad_guess" not in joined
