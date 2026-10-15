"""TASK-06-C acceptance: source changes during review never reach a final write.

Drives the real complete_task with its real origin proof, consumer checks, and the
final origin recheck. The origin and consuming Task rows live in a real
InMemoryTaskGraphStore, and a source change is a real store update made during the
review call. Only the model call, validity capture, graph capture, review checks,
evidence append, and the central commit boundary are stubbed. The commit-level
compare-and-set is covered against a real CentralStateStore in its own test file.

Expected results on the current source:
- test_valid_unchanged_review_adoption_succeeds: an unchanged origin adopted by Task B
  passes the real review; one receipt, one completion, and one audit record are written.
- test_source_change_during_review_writes_no_receipt_or_completion: the origin changes
  during review, so the final recheck refuses with a known code and writes nothing,
  including no review audit record.
- test_control_source_drift_before_review_refuses_before_model_call: pre-review drift
  refuses with a known code before any model call, and nothing is written.
- test_real_final_commit_origin_change_rejects_natively_and_rolls_back: a source change
  inside the real final transaction yields the native rejection, rolls back, and queues no
  TaskCompleted row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.events import EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration import central_results, evidence_validity
from redteam.orchestration import semantic_completion as sc
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.completion import ORIGIN_CHANGED_CODE
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.semantic_completion import complete_task, purpose_hash
from redteam.orchestration.task_graph import InMemoryTaskGraphStore

RUN = "run-accept-1"
ENGAGEMENT = "eng-accept-1"
MISSION = "mission-accept-1"
TASK_A = "task-origin-accept"
TASK_B = "task-consumer-accept"
ACTION = "action-accept-1"
INVOCATION = "invocation-accept-1"
EVIDENCE = "ev-accept-1"
WORK = "work-accept-1"
TOOL = "web.http_get"
OBJECTIVE_A = "confirm the login session behavior on node web-1"
OBJECTIVE_B = "judge whether the admin path reuses the session cookie"
DRIFTED = "a different objective"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mission_plan() -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId(MISSION),
        engagement_id=EngagementId(ENGAGEMENT),
        objective="Assess the session behavior",
        priorities=["session"],
        success_criteria=["evidence-backed result"],
        initial_workstreams=["web assessment"],
        assumptions=["test tenant available"],
        operator_constraints={},
        operator_preferences={},
        status=MissionStatus.ACTIVE,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="operator"),
    )


def _task(task_id: str, objective: str, status: TaskStatus) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId(MISSION),
        title=task_id,
        objective=objective,
        status=status,
        priority=0,
        parent_task_id=None,
        dependency_task_ids=[],
        blocking_reason=None,
        provenance=Provenance(source="planner"),
        created_at=NOW,
        updated_at=NOW,
    )


def _origin_metadata():
    return {
        "execution_origin_version": 1,
        "model_tool_call_id": "call-accept-1",
        "mission_task_id": TASK_A,
        "mission_id": MISSION,
        "task_purpose_hash": purpose_hash(OBJECTIVE_A),
        "work_id": WORK,
        "candidate_key": "cand-accept-1",
        "action_id": ACTION,
        "invocation_id": INVOCATION,
        "tool_id": TOOL,
        "target_node_id": "node-web-1",
        "proposal": {
            "action_id": ACTION,
            "run_id": RUN,
            "engagement_id": ENGAGEMENT,
            "tool_id": TOOL,
            "binding_mode": "selected_tool",
            "arguments": {"path": "/login"},
        },
        "tool_result": {
            "action_id": ACTION,
            "invocation_id": INVOCATION,
            "tool_id": TOOL,
            "success": True,
        },
    }


def _origin_record(meta):
    return EvidenceRecord(
        evidence_id=EVIDENCE,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source_type=EvidenceSourceType.TOOL,
        source_id="tool.web",
        action_id=ACTION,
        invocation_id=INVOCATION,
        content_type="application/json",
        digest=content_hash(meta),
        summary="origin evidence for the review acceptance test",
        created_at=datetime.now(timezone.utc),
        metadata=meta,
    )


class _Store:
    """Evidence store fake: evidence is append-only, so only lookups and appends matter here."""

    def __init__(self, record):
        self._record = record
        self.appends = []

    async def get(self, evidence_id):
        return self._record if str(evidence_id) == EVIDENCE else None

    async def append(self, record):
        self.appends.append(record)

    async def list_for_run(self, run_id):
        return [self._record]


class _Work:
    async def get(self, work_id):
        if work_id != WORK:
            return None
        return SimpleNamespace(
            run_id=RUN,
            mission_task_id=TASK_A,
            candidate_key="cand-accept-1",
            model_tool_call_id="call-accept-1",
            task_purpose_hash=purpose_hash(OBJECTIVE_A),
            execution_origin_version=1,
            target_node_ids=["node-web-1"],
        )


class _Validity:
    async def refresh(self, deps):
        return self, []

    def check(self, tx):
        return None


class _Engine:
    def __init__(self, deps, on_review=None):
        self.deps = deps
        self._on_review = on_review
        self.asks = 0
        self.reviews = []

    async def _review_token(self, runtime):
        return ("token-accept",)

    async def review_context(self, runtime, context, evidence_ids):
        return {}

    async def _ask(self, **kwargs):
        self.asks += 1
        if self._on_review is not None:
            await self._on_review()
        return {
            "accepted": True,
            "reason": "independent review accepted",
            "claims": [],
            "criterion_results": [],
        }

    async def record_completion_review(self, stage, path, runtime, review, evidence_ids, *,
                                       step_id=None):
        self.reviews.append(stage)


@pytest.fixture
def stubs(monkeypatch):
    completed = []

    async def _graph(*args, **kwargs):
        return {}

    async def _no_errors(*args, **kwargs):
        return []

    async def _capture_validity(deps, evidence_ids, **kwargs):
        return _Validity(), []

    async def _complete(deps, runtime, task, token, receipt, proposal, **kwargs):
        completed.append((str(task.task_id), str(receipt.evidence_id)))

    monkeypatch.setattr(sc, "AttackGraphService", lambda store: None)
    monkeypatch.setattr(sc, "capture_result_graph", _graph)
    monkeypatch.setattr(sc, "check_task_conditions", _no_errors)
    monkeypatch.setattr(sc, "check_result_review", _no_errors)
    monkeypatch.setattr(sc, "_pending_execution_errors", _no_errors)
    monkeypatch.setattr(evidence_validity, "capture_validity", _capture_validity)
    monkeypatch.setattr(central_results, "complete_reviewed_result", _complete)
    return completed


async def _run(consumer_objective, *, drift="none"):
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission_plan())
    await store.create_task(_task(TASK_A, OBJECTIVE_A, TaskStatus.COMPLETED))
    await store.create_task(_task(TASK_B, consumer_objective, TaskStatus.RUNNING))
    evidence = _Store(_origin_record(_origin_metadata()))
    deps = SimpleNamespace(
        evidence_store=evidence,
        path_store=None,
        graph_store=None,
        investigations=store,
        task_graph_store=store,
        work_store=_Work(),
    )

    async def _drift_source():
        # A real store update of the origin Task row, through the public store API.
        current = await store.get_task(TaskId(TASK_A))
        await store.update_task(current.model_copy(update={"objective": DRIFTED}))

    if drift == "before":
        await _drift_source()
    engine = _Engine(deps, on_review=_drift_source if drift == "during" else None)
    decision = SimpleNamespace(
        completion={"outcome": "success", "reason": "review-ready", "evidence_ids": [EVIDENCE]},
        reason="review-ready",
    )
    runtime = SimpleNamespace(
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        active_task_id=TASK_B,
        work_owner="owner-accept",
        task_ownership_version=1,
    )
    result = await complete_task(engine, None, decision, runtime, None)
    return result, engine, evidence


@pytest.mark.asyncio
async def test_valid_unchanged_review_adoption_succeeds(stubs):
    # Task B adopts Task A's unchanged original for its own purpose. The real review
    # runs once, then one receipt, one completion, and one audit record are written.
    result, engine, evidence = await _run(OBJECTIVE_B)
    assert result == []
    assert engine.asks == 1
    assert engine.reviews == ["task_completion_review"]
    assert len(evidence.appends) == 1
    receipt = evidence.appends[0].metadata
    assert receipt["task_id"] == TASK_B
    assert receipt["purpose_hash"] == purpose_hash(OBJECTIVE_B)
    assert receipt["source_evidence_ids"] == [EVIDENCE]
    assert len(stubs) == 1


@pytest.mark.asyncio
async def test_source_change_during_review_writes_no_receipt_or_completion(stubs):
    # The origin Task row changes while the review runs. The final recheck refuses
    # with the known code before the receipt, the completion, or the audit record.
    result, engine, evidence = await _run(OBJECTIVE_B, drift="during")
    assert result
    assert result.diagnostics[0].code == ORIGIN_CHANGED_CODE
    assert engine.asks == 1
    assert engine.reviews == []
    assert evidence.appends == []
    assert stubs == []


@pytest.mark.asyncio
async def test_control_source_drift_before_review_refuses_before_model_call(stubs):
    # Pre-review drift already refuses before any model call, and nothing is written.
    result, engine, evidence = await _run(OBJECTIVE_B, drift="before")
    assert result
    assert result.diagnostics[0].code == "origin_purpose_changed"
    assert engine.asks == 0
    assert engine.reviews == []
    assert evidence.appends == []
    assert stubs == []


@pytest.mark.asyncio
async def test_final_transaction_origin_conflict_returns_native_rejection(stubs, monkeypatch):
    # The final transaction raises the exact carrier, and the semantic boundary turns it
    # into the native CompletionIssues rejection with the producer diagnostic intact.
    from redteam.orchestration.completion import origin_task_changed_diagnostic

    async def _final_conflict(deps, runtime, task, token, receipt, proposal, **kwargs):
        diagnostic = origin_task_changed_diagnostic(
            [EVIDENCE], owner="orchestration.central_results",
        )
        raise central_results.OriginTaskChangedConflict(TASK_A, diagnostic)

    monkeypatch.setattr(central_results, "complete_reviewed_result", _final_conflict)
    result, engine, evidence = await _run(OBJECTIVE_B)
    assert result
    assert result.diagnostics[0].code == ORIGIN_CHANGED_CODE
    assert result.diagnostics[0].evidence_ids == [EVIDENCE]
    assert result.diagnostics[0].owner == "orchestration.central_results"
    assert engine.reviews == ["task_completion_review"]
    assert stubs == []


class _IntegrityStore(_Store):
    """Evidence fake for the real final chain: integrity passes and this run's receipt resolves."""

    async def verify_run_integrity(self, run_id):
        return []

    async def get(self, evidence_id):
        if str(evidence_id) == EVIDENCE:
            return self._record
        return next((r for r in self.appends if str(r.evidence_id) == str(evidence_id)), None)


@pytest.mark.asyncio
async def test_real_final_commit_origin_change_rejects_natively_and_rolls_back(
    tmp_path, monkeypatch,
):
    # The origin row changes only inside the real final transaction, after the real review
    # and receipt. The commit refuses, the native CompletionIssues carries the producer
    # diagnostic, every Task write rolls back, and no TaskCompleted row is queued.
    state = CentralStateStore(tmp_path / 'state.db', run_id=RUN, engagement_id=ENGAGEMENT)
    await state.setup()
    await state.create_mission(_mission_plan())
    await state.bind_run(MISSION, scope=EngagementScope(), binding_hash='c' * 64)
    await state.register_task('register-origin', _task(TASK_A, OBJECTIVE_A, TaskStatus.READY))
    await state.register_task('register-consumer', _task(TASK_B, OBJECTIVE_B, TaskStatus.READY))
    claimed = await state.claim_task(TASK_B, owner='owner-accept', instance_id='instance-accept')
    await state.transaction(lambda tx: tx.entering(
        TASK_B, owner='owner-accept', version=claimed.ownership_version,
        instance_id='instance-accept',
    ))
    origin_before = content_hash(await state.get_task(TASK_A))
    consumer_before = content_hash(await state.get_task(TASK_B))
    evidence = _IntegrityStore(_origin_record(_origin_metadata()))

    async def _flush():
        return None

    deps = SimpleNamespace(
        central_state=state,
        investigations=state,
        task_graph_store=state,
        work_store=_Work(),
        evidence_store=evidence,
        path_store=None,
        graph_store=None,
        event_bus=SimpleNamespace(flush=_flush),
    )

    class _StateEngine(_Engine):
        async def _review_token(self, runtime):
            def read(tx):
                current = tx.admit()
                return current.revision, content_hash(current)

            return await state.transaction(read, write=False)

    async def _empty(*args, **kwargs):
        return []

    async def _no_graph(*args, **kwargs):
        return {}

    async def _capture_validity(_deps, _evidence_ids, **kwargs):
        return _Validity(), []

    monkeypatch.setattr(sc, 'AttackGraphService', lambda store: None)
    monkeypatch.setattr(sc, 'capture_result_graph', _no_graph)
    monkeypatch.setattr(sc, 'check_task_conditions', _empty)
    monkeypatch.setattr(sc, 'check_result_review', _empty)
    monkeypatch.setattr(evidence_validity, 'capture_validity', _capture_validity)

    # Only the final commit is wrapped: its transaction stages a change to the origin row
    # before the commit callback runs the origin compare-and-set.
    original_transaction = state.transaction
    original_commit = central_results.commit_result
    staged = []

    async def _commit_with_final_source_change(*args, final, **kwargs):
        if not final:
            return await original_commit(*args, final=final, **kwargs)

        async def _transaction_with_source_change(callback, *, write=True):
            def with_changed_source(tx):
                current = tx.get_task(TASK_A)
                tx._write_task(current.model_copy(update={'title': 'Changed at final CAS'}))
                staged.append(TASK_A)
                return callback(tx)

            return await original_transaction(with_changed_source, write=write)

        monkeypatch.setattr(state, 'transaction', _transaction_with_source_change)
        try:
            return await original_commit(*args, final=final, **kwargs)
        finally:
            monkeypatch.setattr(state, 'transaction', original_transaction)

    monkeypatch.setattr(central_results, 'commit_result', _commit_with_final_source_change)

    engine = _StateEngine(deps)
    decision = SimpleNamespace(
        completion={'outcome': 'success', 'reason': 'review-ready', 'evidence_ids': [EVIDENCE]},
        reason='review-ready',
    )
    runtime = SimpleNamespace(
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        active_task_id=TASK_B,
        work_owner='owner-accept',
        task_ownership_version=claimed.ownership_version,
    )
    result = await complete_task(engine, None, decision, runtime, None)

    assert staged == [TASK_A]
    assert result
    diagnostic = result.diagnostics[0]
    assert diagnostic.code == 'origin_task_changed_reassessment_required'
    assert diagnostic.cause_scope == 'evidence_validity'
    assert diagnostic.owner == 'orchestration.central_results'
    assert diagnostic.evidence_ids == [EVIDENCE]
    assert content_hash(await state.get_task(TASK_A)) == origin_before
    assert content_hash(await state.get_task(TASK_B)) == consumer_before
    completed = await state.transaction(lambda tx: tx.db.execute(
        'SELECT COUNT(*) FROM state_outbox WHERE run_id=? AND '
        "json_extract(payload, '$.event_type')=?",
        (RUN, EventType.TASK_COMPLETED),
    ).fetchone()[0], write=False)
    assert completed == 0
