"""Synthetic canonical records: bounded reads do not replace admission or originals."""

import copy
import json
import sqlite3
from datetime import timedelta

import pytest
from pydantic import ValidationError

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.central import GraphTransaction
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.completion import CompletionCriterion
from redteam.core.contracts.execution import TaskExecution
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import GraphEdgeType, GraphNodeType, TaskStatus
from redteam.orchestration.investigation_context import (
    ContextQuery,
    ContextReadError,
    ContextRef,
    InvestigationContextReader,
    InvestigationSnapshot,
    capture_investigation_context,
    context_bytes,
)
from tests.fakes.assessment_drafts import draft
from tests.unit.test_central_state import BINDING, ENG, NOW, RUN, mission, task, world


def candidate():
    return Hypothesis(contract_version=2, hypothesis_id="c", description="Possible access issue",
                      target_node_ids=["host:a"], reason="Inspect a related response",
                      revision=2, status="held", supporting_evidence_ids=["original-old"])


def execution(identity="inv-old", **changes):
    return TaskExecution.model_validate({
        "run_id": RUN, "engagement_id": ENG, "task_id": "blocked",
        "instance_id": "worker", "ownership_version": 1,
        "action_id": "action-old", "invocation_id": identity,
        "model_tool_call_id": identity, "tool_id": "provider.inspect",
        "input_digest": "a" * 64, "proposal_evidence_id": "proposal-old",
        "state": "unknown", **changes,
    })


def snapshot(*, historical=10, long=False):
    source = task("source").model_copy(update={
        "status": TaskStatus.COMPLETED, "required": False,
        "related_attack_graph_node_ids": ["host:a"],
        "result": InvestigationResult.model_validate({
            "result_id": "source-result", "outcome": "inconclusive",
            "contract_version": 2,
            "summary": "Received a response", "observations": ["Scoped negative observation"],
            "limitations": ["Other principals not tested"], "evidence_ids": ["original-old"],
        }),
    })
    active = task("active").model_copy(update={
        "status": TaskStatus.RUNNING, "related_attack_graph_node_ids": ["host:a"],
        "completion_contract_version": 2,
        "investigation_contract_version": 2,
        "related_hypothesis_ids": ["c"], "hypothesis_revisions": {"c": 2},
        "source_task_id": "source", "source_result_id": "source-result",
        "dependency_task_ids": ["source"],
        "completion_criteria": [CompletionCriterion(kind="observation",
                                                    description="Observe response")],
    })
    # Revalidate model_copy data so every fixture obeys the real legacy contract.
    active = type(active).model_validate(active.model_dump())
    blocked = task("blocked").model_copy(update={
        "status": TaskStatus.BLOCKED, "blocking_reason": "Await original execution recovery",
    })
    history = [task(f"old-{i:04d}").model_copy(update={
        "status": TaskStatus.COMPLETED, "required": False,
        "title": "Historical check", "objective": "Former purpose " + "x" * (2000 if long else 80),
        "updated_at": NOW + timedelta(seconds=i),
    }) for i in range(historical)]
    return InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=[active, blocked, source, *history], candidates=[candidate()],
        executions=[execution()],
    )


def read(kind, identity, **kwargs):
    return ContextQuery(kind=kind, mode="read", refs=(ContextRef(kind=kind, identity=identity),),
                        **kwargs)


def test_frontier_size_is_bounded_and_old_source_details_remain_readable():
    small = InvestigationContextReader(snapshot(historical=10))
    large = InvestigationContextReader(snapshot(historical=1000, long=True))
    view = large.overview(active_task_id="active", max_bytes=8192)
    assert view["status"] == "ok" and context_bytes(view) <= 8192
    assert view["summary"]["states"]["task"]["blocked"] == 1
    assert view["summary"]["unsafe_executions"] == 1
    assert view["summary"]["required_incomplete_tasks"] == 2
    assert view["omitted_counts"]["task"] >= 995
    assert view["active_contract"]["completion_criteria"][0]["description"] == "Observe response"
    assert "original-old" in json.dumps(view)
    assert context_bytes(view) < context_bytes(small.overview(active_task_id="active")) + 4096
    old = large.query(ContextQuery(text="Former purpose", count=2, max_bytes=4096))
    assert old["status"] == "ok" and old["count"] == 1000 and old["continuation"]
    detail = large.query(read("task", "source", section="result"))
    assert detail["records"][0]["data"]["result_id"] == "source-result"
    assert detail["records"][0]["data"]["limitations"] == ["Other principals not tested"]
    candidate_view = large.query(read("candidate", "c"))
    assert candidate_view["records"][0]["data"]["revision"] == 2
    assert candidate_view["records"][0]["data"]["status"] == "held"


def test_task_card_keeps_draft_count_identity_and_detail_pointer():
    captured = snapshot(historical=0)
    tasks = [
        type(task()).model_validate_json(record.payload)
        for record in captured.records if record.kind == "task"
    ]
    source = next(item for item in tasks if str(item.task_id) == "source")
    assessment = AssessmentDraft.model_validate(draft(
        target="host:a", evidence_id="original-old", statement="Qualified assessment claim",
    ))
    source = source.model_copy(update={
        "result": source.result.model_copy(update={"assessment_drafts": [assessment]})
    })
    tasks = [source if str(item.task_id) == "source" else item for item in tasks]
    reader = InvestigationContextReader(InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=tasks, candidates=[candidate()], executions=[execution()],
    ))
    listed = reader.query(ContextQuery(kind="task", count=8, max_bytes=16384))
    card = next(item for item in listed["records"] if item["identity"] == "source")
    result = card["data"]["result"]
    assert result["assessment_draft_count"] == 1
    assert result["assessment_drafts_omitted_count"] == 0
    assert result["assessment_drafts"][0]["draft_id"] == "draft-a"
    assert len(result["assessment_drafts"][0]["content_hash"]) == 64
    assert result["assessment_drafts_detail"] == {
        "kind": "task", "identity": "source", "section": "result",
    }
    detail = reader.query(read("task", "source", section="result", max_bytes=65536))
    assert detail["records"][0]["data"]["assessment_drafts"][0]["statement"] == (
        "Qualified assessment claim"
    )


def test_pages_are_bound_to_query_scope_snapshot_and_do_not_lose_records():
    captured = snapshot(historical=8)
    reader = InvestigationContextReader(captured)
    query = ContextQuery(text="Former purpose", count=2, snapshot_id=reader.snapshot_id)
    page = reader.query(query)
    identities = {card["identity"] for card in page["records"]}
    cursor = page["continuation"]
    while cursor:
        page = reader.query(query.model_copy(update={"continuation": cursor}))
        assert page["status"] == "ok"
        identities.update(card["identity"] for card in page["records"])
        cursor = page["continuation"]
    assert len(identities) == 8
    original_cursor = reader.query(query)["continuation"]
    wrong_query = query.model_copy(update={"text": "different", "continuation": original_cursor})
    assert reader.query(wrong_query)["code"] == "context_continuation_scope_or_snapshot_changed"
    changed = InvestigationContextReader(snapshot(historical=9))
    assert changed.query(query)["code"] == "context_snapshot_changed"
    continued = query.model_copy(update={"snapshot_id": None, "continuation": original_cursor})
    assert changed.query(continued)["status"] == "error"
    worker = InvestigationContextReader(captured, worker_task_id="active")
    assert worker.query(query.model_copy(update={
        "continuation": original_cursor,
    }))["status"] == "error"


def test_relationship_lookup_finds_source_and_candidate_without_whole_inventory():
    reader = InvestigationContextReader(snapshot(historical=100))
    query = ContextQuery.model_validate({
        "kind": "task", "mode": "related", "refs": [{"kind": "task", "identity": "active"}],
        "hops": 1,
    })
    result = reader.query(query)
    assert {card["identity"] for card in result["records"]} == {"active", "source"}
    target = reader.query(ContextQuery(
        mode="related", refs=(ContextRef(kind="node", identity="host:a"),),
    ))
    assert {card["identity"] for card in target["records"]} == {"active", "source"}
    stale = reader.query(ContextQuery(hypothesis_revisions={"c": 1}))
    assert stale["code"] == "context_candidate_revision_changed"
    assert reader.query(read("task", "absent"))["code"] == "context_reference_not_found"


def test_worker_scope_does_not_open_siblings_or_reinterpret_current_candidate():
    captured = snapshot()
    worker = InvestigationContextReader(captured, worker_task_id="active")
    assert worker.query(read("task", "source"))["status"] == "ok"
    assert worker.query(read("task", "blocked"))["code"] == "context_reference_outside_read_scope"
    assert worker.query(read("task", "old-0001"))["status"] == "error"
    data = candidate().model_copy(update={"revision": 3})
    changed = InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=[type(task()).model_validate_json(record.payload) for record in captured.records
               if record.kind == "task"], candidates=[data], executions=[execution()],
    )
    with pytest.raises(ContextReadError, match="candidate_revision_unavailable"):
        InvestigationContextReader(changed, worker_task_id="active")


def test_full_contract_overflow_is_visible_and_never_sliced():
    item = task("active").model_copy(update={
        "objective": "x" * 15000,
        "completion_contract_version": 2,
        "completion_criteria": [CompletionCriterion(kind="observation", description="y" * 20000)],
    })
    item = type(item).model_validate(item.model_dump())
    before = copy.deepcopy(item.model_dump())
    captured = InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=[item], candidates=[], executions=[],
    )
    reader = InvestigationContextReader(captured)
    assert reader.overview(active_task_id="active", max_bytes=2048)["code"] == (
        "context_required_contract_too_large"
    )
    assert reader.query(read("task", "active", max_bytes=2048))["code"] == (
        "context_detail_too_large"
    )
    full = reader.query(read("task", "active", section="completion_criteria", max_bytes=65536))
    assert full["records"][0]["data"][0]["description"] == "y" * 20000
    assert item.model_dump() == before
    item.objective = "mutated after capture"
    assert reader.query(read("task", "active", max_bytes=65536))["records"][0]["data"][
        "objective"] == "x" * 15000
    full["records"][0]["data"][0]["description"] = "mutated response"
    assert reader.query(read("task", "active", section="completion_criteria", max_bytes=65536))[
        "records"][0]["data"][0]["description"] == "y" * 20000


@pytest.mark.parametrize("fields", [
    {"count": True}, {"count": 0}, {"max_bytes": 1024}, {"hops": 20},
    {"target_node_ids": ["foreign\x1fhost:a"]}, {"statuses": ["refuted"]},
    {"hypothesis_revisions": {"c": True}}, {"sql": "SELECT *"},
])
def test_queries_reject_invalid_or_unbounded_inputs(fields):
    with pytest.raises(ValidationError):
        ContextQuery.model_validate(fields)


def test_succeeded_job_acknowledgement_is_still_unsafe_and_not_recent_by_id():
    item = execution("inv-zz", state="succeeded", job_ref="job-live", wait_reason="job")
    captured = InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=[task("blocked")], candidates=[], executions=[item],
    )
    reader = InvestigationContextReader(captured)
    result = reader.query(ContextQuery(kind="execution"))
    assert result["summary"]["unsafe_executions"] == 1
    assert result["records"][0]["data"]["unsafe"] is True
    assert result["records"][0]["data"]["recency"] == "unknown"
    assert result["records"][0]["data"]["job_ref"] == "job-live"


def test_many_open_records_still_fit_and_unknown_selectors_are_not_empty_success():
    items = [task(f"blocked-{index}").model_copy(update={
        "status": TaskStatus.BLOCKED, "blocking_reason": "Await approval",
        "related_attack_graph_node_ids": ["host:a"],
        "objective": "한글" * 1000,
    }) for index in range(200)]
    candidates = [candidate().model_copy(update={
        "hypothesis_id": f"candidate-{index}", "status": status,
    }) for index, status in enumerate(("confirmed", "rejected", "inconclusive"))]
    reader = InvestigationContextReader(InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=items, candidates=candidates, executions=[],
    ))
    view = reader.overview(max_bytes=4096)
    assert view["status"] == "ok" and context_bytes(view) <= 4096
    assert view["summary"]["states"]["task"]["blocked"] == 200
    assert view["summary"]["required_incomplete_tasks"] == 200
    assert view["omitted_counts"]["task"] >= 196
    for identity in ("candidate-0", "candidate-1"):
        assert reader.query(read("candidate", identity))["status"] == "ok"
    related = reader.query(ContextQuery(
        mode="related", refs=(ContextRef(kind="node", identity="host:a"),),
        max_visited=4,
    ))
    assert related["relation_truncated"]
    assert reader.query(ContextQuery(target_node_ids=("host:absent",)))["status"] == "error"
    assert reader.query(ContextQuery(principal_refs=("absent",)))["status"] == "error"


def test_snapshot_rejects_foreign_execution_and_detail_keeps_read_boundaries():
    with pytest.raises(ContextReadError, match="execution_scope_mismatch"):
        InvestigationSnapshot.create(
            run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
            tasks=[task("blocked")], candidates=[],
            executions=[execution(run_id="different")],
        )
    reader = InvestigationContextReader(snapshot())
    result = reader.query(read("task", "source", statuses=("running",)))
    assert result["code"] == "context_record_filter_mismatch"
    missing = reader.query(ContextQuery(continuation="invalid"))
    assert missing["status"] == "error"


def test_execution_filters_use_explicit_parent_task_relations():
    related = task("blocked").model_copy(update={
        "related_attack_graph_node_ids": ["host:a"], "related_hypothesis_ids": ["c"],
        "hypothesis_revisions": {"c": 2}, "principal_refs": ["account-a"],
        "investigation_contract_version": 2, "completion_contract_version": 2,
        "completion_criteria": [CompletionCriterion(
            kind="observation", description="Inspect account response",
            principal_refs=["account-a"],
        )],
    })
    reader = InvestigationContextReader(InvestigationSnapshot.create(
        run_id=RUN, engagement_id=ENG, mission=mission(), binding_hash=BINDING,
        tasks=[related], candidates=[candidate()], executions=[execution()],
    ))
    result = reader.query(ContextQuery(
        kind="execution", target_node_ids=("host:a",), principal_refs=("account-a",),
        hypothesis_revisions={"c": 2},
    ))
    assert result["status"] == "ok" and result["count"] == 1
    assert result["records"][0]["identity"] == "inv-old"


async def test_real_central_capture_scoped_graph_cycle_dense_and_no_writes(tmp_path, monkeypatch):
    state = await world(tmp_path)
    service = AttackGraphService(build_central_graph(state))
    for index in range(9):
        await service.upsert_node(GraphNodeType.HOST, key=str(index))
    for index in range(1, 9):
        await service.link(GraphEdgeType.RELATED_TO, "host:0", f"host:{index}")
    await service.link(GraphEdgeType.RELATED_TO, "host:2", "host:1")
    await service.upsert_node(GraphNodeType.CREDENTIAL, key="unrelated")
    await state.register_task("register-active", task("active"))
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE state_graph_nodes SET payload='invalid' WHERE node_type='credential'")
        before = db.total_changes
    def forbidden(*args, **kwargs):
        raise AssertionError("bounded lookup must not materialize the full graph")
    monkeypatch.setattr(GraphTransaction, "list_nodes", forbidden)
    monkeypatch.setattr(GraphTransaction, "list_edges", forbidden)
    snapshot = await capture_investigation_context(
        state, graph_roots=("host:0",), hops=3, max_nodes=4, max_edges=4,
    )
    assert snapshot.graph_truncated
    assert sum(record.kind == "node" for record in snapshot.records) <= 4
    assert sum(record.kind == "edge" for record in snapshot.records) <= 4
    reader = InvestigationContextReader(snapshot)
    assert reader.query(read("node", "host:0"))["status"] == "ok"
    assert reader.query(read("node", "credential:unrelated"))["status"] == "error"
    assert len(await state.list_tasks()) == 1
    with sqlite3.connect(state.path) as db:
        assert db.total_changes == 0
    assert before == 1  # Only the fixture's explicit corruption wrote anything.
    with pytest.raises(ValueError, match="root_not_found"):
        await capture_investigation_context(state, graph_roots=("host:absent",))


async def test_real_capture_pagination_rejects_new_snapshot_but_old_snapshot_is_immutable(tmp_path):
    state = await world(tmp_path)
    for index in range(3):
        await state.register_task(f"register-{index}", task(f"task-{index}"))
    first = await capture_investigation_context(state)
    query = ContextQuery(count=1)
    old = InvestigationContextReader(first)
    page = old.query(query)
    await state.register_task("register-new", task("task-new"))
    changed = InvestigationContextReader(await capture_investigation_context(state))
    continued = query.model_copy(update={"continuation": page["continuation"]})
    assert old.query(continued)["status"] == "ok"
    assert changed.query(continued)["status"] == "error"
    other = await world(tmp_path, run="other", engagement="other-eng", mission_id="other-mission")
    foreign = InvestigationContextReader(await capture_investigation_context(other))
    assert foreign.query(continued)["status"] == "error"
    assert foreign.query(read("task", "task-0"))["status"] == "error"
