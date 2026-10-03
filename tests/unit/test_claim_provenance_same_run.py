"""Same-run claim provenance projection (new, no legacy fixtures)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


#
async def test_collect_knowledge_matches_display_and_carries_full_refs():
    from redteam.orchestration.knowledge import collect_assessment, collect_knowledge
    from redteam.orchestration.knowledge_validation import assessment_blocked

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
    graph = _Graph([_node("host:good-1", good)])
    ev = _Evidence([_record("ev-good", RUN, {"good_val": "keep"})])
    display, snapshot = await collect_knowledge(graph, ev, RUN, step=1)
    assert len(display["facts"]) == 1
    assert "host:good-1#fact:good_val" in snapshot.fact_refs
    assert "evidence:ev-good" in snapshot.fact_refs
    assert assessment_blocked(snapshot) is None
    only = await collect_assessment(graph, ev, RUN, step=1)
    assert only.fact_refs == snapshot.fact_refs


#
async def test_snapshot_marks_failed_reads_not_empty():
    from redteam.orchestration.knowledge import collect_assessment
    from redteam.orchestration.knowledge_validation import assessment_blocked

    class _BadEvidence:
        async def list_for_run(self, run_id):
            raise RuntimeError("store down")

    class _BadGraph(_Graph):
        async def list_nodes(self):
            raise RuntimeError("graph down")

    failed = await collect_assessment(_Graph([]), _BadEvidence(), RUN)
    assert failed.evidence_lookup_failed is True
    assert assessment_blocked(failed) is not None
    failed_graph = await collect_assessment(_BadGraph([]), _Evidence([]), RUN)
    assert failed_graph.graph_lookup_failed is True
    assert assessment_blocked(failed_graph) is not None


#
async def test_mis_scoped_record_never_grounds():
    from redteam.orchestration.knowledge import collect_assessment

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

    class _LeakyEvidence(_Evidence):
        async def list_for_run(self, run_id):
            return list(self._records)

    graph = _Graph([_node("host:good-1", good)])
    leaky = _LeakyEvidence([_record("ev-good", OTHER_RUN, {"good_val": "keep"})])
    snapshot = await collect_assessment(graph, leaky, RUN, step=1)
    assert "evidence:ev-good" not in snapshot.fact_refs
    assert "host:good-1#fact:good_val" not in snapshot.fact_refs


#
@pytest.mark.parametrize("kind", ["bare_evidence_id", "target_cap", "evidence_cap"])
async def test_display_and_snapshot_ref_rules(kind):
    from redteam.orchestration.knowledge import collect_knowledge

    if kind == "bare_evidence_id":
        props = build_properties(
            facts={"good_val": "keep", "evidence_ids": ["ev-bare"]},
            provenance=Provenance(
                step=1,
                source="tool:probe",
                evidence_id="ev-good",
                run_id=RUN,
                confidence=Confidence.MEASURED,
            ),
            step=1,
        )
        graph = _Graph([_node("host:bare-1", props)])
        ev = _Evidence([_record("ev-good", RUN, {"good_val": "keep"})])
        display, snapshot = await collect_knowledge(graph, ev, RUN, step=1)
        assert len(display["facts"]) == 1
        assert "evidence:ev-bare" not in display["fact_refs"]
        assert "evidence:ev-good" in display["fact_refs"]
        assert "ev-bare" in display["facts"][0]["evidence_ids"]
        assert "evidence:ev-bare" not in snapshot.fact_refs
        return
    if kind == "target_cap":
        nodes = []
        for i in range(101):
            props = build_properties(
                attrs={"in_scope": True},
                provenance=Provenance(
                    step=0,
                    source="engagement:scope",
                    run_id=RUN,
                    confidence=Confidence.SERVER_DECLARED,
                ),
                step=0,
            )
            nodes.append(_node("host:scope-" + str(i), props))
        graph = _Graph(nodes)
        display, snapshot = await collect_knowledge(graph, _Evidence([]), RUN)
        assert len(display["provided"]["claims"]) == 50
        assert len(display["provided"]["omitted_node_ids"]) == 51
        assert len(snapshot.targets) == 101
        assert "host:scope-100" in snapshot.targets
        return
    merged = None
    records = []
    for i in range(25):
        part = build_properties(
            facts={"val-" + str(i): i},
            provenance=Provenance(
                step=1,
                source="tool:probe",
                evidence_id="ev-" + str(i),
                run_id=RUN,
                confidence=Confidence.MEASURED,
            ),
            step=1,
        )
        merged = part if merged is None else merge_properties(merged, part)
        records.append(_record("ev-" + str(i), RUN, {"val": i}))
    graph = _Graph([_node("host:many-1", merged)])
    display, snapshot = await collect_knowledge(graph, _Evidence(records), RUN, step=1)
    shown = [r for r in display["fact_refs"] if r.startswith("evidence:")]
    taken = [r for r in snapshot.fact_refs if r.startswith("evidence:")]
    assert len(shown) == 20
    assert len(taken) == 25
