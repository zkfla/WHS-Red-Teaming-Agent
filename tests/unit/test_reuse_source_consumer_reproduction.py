"""TASK-06-C reuse reproduction: origin purpose versus consuming Task purpose.

A legitimate follow-up adopts an unchanged original that Task A produced. The origin
proof checks that original's own stored purpose against Task A. The reuse paths now
use prove_original, which never compares against the consuming Task. Before the
TASK-06-C change, verify_single_origin and _verify_task_origin compared the stored
purpose with the consuming Task B, so a valid adoption was rejected as
"purpose changed". The two reproductions were that failing evidence and now pass for
the same reason. The control tests keep their expectations. The legacy caller control
pins the consuming compare that verify_single_origin(task=...) still performs for
code artifacts and the legacy goal chain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.completion import origin_row_hash
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.evidence_validity import prove_original
from redteam.orchestration.semantic_completion import (
    TASK_RECEIPT_KIND,
    _verify_task_origin,
    purpose_hash,
    verify_single_origin,
    verify_task_receipt,
)

RUN = "run-reuse-1"
ENGAGEMENT = "eng-reuse-1"
MISSION = "mission-reuse-1"
TASK_A = "task-origin-a"
TASK_B = "task-consumer-b"
ACTION = "action-reuse-1"
INVOCATION = "invocation-reuse-1"
EVIDENCE = "ev-origin-reuse-1"
WORK = "work-reuse-1"
TOOL = "web.http_get"
OBJECTIVE_A = "confirm the login session behavior on node web-1"
OBJECTIVE_B = "judge whether the admin path reuses the session cookie"


def _origin_metadata(**overrides):
    meta = {
        "execution_origin_version": 1,
        "model_tool_call_id": "call-reuse-1",
        "mission_task_id": TASK_A,
        "mission_id": MISSION,
        "task_purpose_hash": purpose_hash(OBJECTIVE_A),
        "work_id": WORK,
        "candidate_key": "cand-reuse-1",
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
    meta.update(overrides)
    return meta


def _origin_record(meta, **overrides):
    fields = {
        "evidence_id": EVIDENCE,
        "run_id": RUN,
        "engagement_id": ENGAGEMENT,
        "source_type": EvidenceSourceType.TOOL,
        "source_id": "tool.web",
        "action_id": ACTION,
        "invocation_id": INVOCATION,
        "content_type": "application/json",
        "digest": content_hash(meta),
        "summary": "origin evidence for the reuse reproduction",
        "created_at": datetime.now(timezone.utc),
        "metadata": meta,
    }
    fields.update(overrides)
    return EvidenceRecord(**fields)


class _EvidenceStore:
    def __init__(self, record):
        self._record = record

    async def get(self, evidence_id):
        return self._record if str(evidence_id) == EVIDENCE else None


class _WorkStore:
    async def get(self, work_id):
        if work_id != WORK:
            return None
        return SimpleNamespace(
            run_id=RUN,
            mission_task_id=TASK_A,
            candidate_key="cand-reuse-1",
            model_tool_call_id="call-reuse-1",
            task_purpose_hash=purpose_hash(OBJECTIVE_A),
            execution_origin_version=1,
            target_node_ids=["node-web-1"],
        )


class _TaskStore:
    def __init__(self, origin_objective=OBJECTIVE_A):
        self._origin = SimpleNamespace(
            task_id=TASK_A,
            mission_id=MISSION,
            objective=origin_objective,
        )

    async def get_task(self, task_id):
        return self._origin if task_id == TASK_A else None


def _consumer(objective=OBJECTIVE_B):
    return SimpleNamespace(
        task_id=TASK_B,
        mission_id=MISSION,
        objective=objective,
        related_attack_graph_node_ids=[],
        related_hypothesis_ids=[],
        principal_refs=[],
        completion_contract_version=1,
    )


async def _verify_single(meta, consumer, *, record=None, task_store=None):
    # The goal reuse path: the original's own proof, never the consuming Task's purpose.
    record_out, proof = await prove_original(
        evidence_store=_EvidenceStore(record if record is not None else _origin_record(meta)),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        evidence_id=EVIDENCE,
        require_proposal=True,
        task_graph_store=task_store if task_store is not None else _TaskStore(),
        work_store=_WorkStore(),
    )
    return record_out, [issue.summary for issue in proof.issues]


@pytest.mark.asyncio
async def test_repro_followup_adopts_unchanged_origin_under_its_own_purpose():
    # Task A's original is unchanged and Task B adopts it for a different purpose.
    # The origin proof passes on A's stored purpose; B's relevance is a separate review.
    record, errors = await _verify_single(_origin_metadata(), _consumer())
    assert errors == []
    assert record is not None


@pytest.mark.asyncio
async def test_public_original_assessment_returns_pre_review_source_row_hash():
    source = _TaskStore()
    source_row = await source.get_task(TASK_A)
    record, proof = await prove_original(
        evidence_store=_EvidenceStore(_origin_record(_origin_metadata())),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        evidence_id=EVIDENCE,
        task_graph_store=source,
        work_store=_WorkStore(),
    )
    assert record is not None
    assert proof.issues == ()
    assert proof.origin_task_id == TASK_A
    assert proof.origin_task_hash == origin_row_hash(source_row)


@pytest.mark.asyncio
async def test_public_original_assessment_retains_safe_known_raw_failure_code():
    metadata = _origin_metadata()
    metadata.pop("tool_result")
    record, proof = await prove_original(
        evidence_store=_EvidenceStore(_origin_record(metadata)),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        evidence_id=EVIDENCE,
        task_graph_store=_TaskStore(),
        work_store=_WorkStore(),
    )
    assert record is None
    assert len(proof.issues) == 1
    diagnostic = proof.issues[0]
    assert diagnostic.code == "origin_raw_missing"
    assert diagnostic.owner == "orchestration.completion"
    assert diagnostic.cause_scope == "evidence_validity"
    assert diagnostic.evidence_ids == [EVIDENCE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "consumer", "principal_scope", "expected_code"),
    [
        (
            _origin_metadata(),
            _consumer(),
            None,
            "origin_target_out_of_scope",
        ),
        (
            _origin_metadata(principal_ref="principal-a"),
            _consumer(),
            {"principal-b"},
            "origin_principal_out_of_scope",
        ),
    ],
)
async def test_task_subject_refusal_keeps_structured_cause_and_origin_ref(
    metadata, consumer, principal_scope, expected_code,
):
    if expected_code == "origin_target_out_of_scope":
        consumer.related_attack_graph_node_ids = ["node-web-2"]
    _record, issues, _edge_proof, _proof = await _verify_task_origin(
        evidence_store=_EvidenceStore(_origin_record(metadata)),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        evidence_id=EVIDENCE,
        task=consumer,
        mission_id=MISSION,
        step_principal_scope=principal_scope,
        task_graph_store=_TaskStore(),
        work_store=_WorkStore(),
    )
    assert issues
    diagnostic, = issues.diagnostics
    assert diagnostic.code == expected_code
    assert diagnostic.owner == "orchestration.semantic_completion"
    assert diagnostic.cause_scope == "target_prerequisite"
    assert diagnostic.evidence_ids == [EVIDENCE]


@pytest.mark.asyncio
async def test_repro_task_origin_adopts_unchanged_origin_under_consuming_task():
    # Same reuse on the complete_task/_verify_task_origin consumer path.
    record, errors, _edge, _proof = await _verify_task_origin(
        evidence_store=_EvidenceStore(_origin_record(_origin_metadata())),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        evidence_id=EVIDENCE,
        task=_consumer(),
        mission_id=MISSION,
        task_graph_store=_TaskStore(),
        work_store=_WorkStore(),
    )
    assert errors == []
    assert record is not None


@pytest.mark.asyncio
async def test_control_same_purpose_reuse_is_admitted():
    record, errors = await _verify_single(_origin_metadata(), _consumer(OBJECTIVE_A))
    assert errors == []
    assert record is not None


@pytest.mark.asyncio
async def test_control_origin_task_purpose_drift_is_rejected():
    record, errors = await _verify_single(
        _origin_metadata(),
        _consumer(),
        task_store=_TaskStore(origin_objective="a different objective"),
    )
    assert record is None
    assert errors and "purpose changed" in errors[0]


@pytest.mark.asyncio
async def test_control_forged_origin_purpose_hash_is_rejected():
    meta = _origin_metadata(task_purpose_hash=purpose_hash("forged objective"))
    record, errors = await _verify_single(meta, _consumer())
    assert record is None
    assert errors


@pytest.mark.asyncio
async def test_control_origin_from_another_run_is_rejected():
    other = _origin_record(_origin_metadata(), run_id="run-other")
    record, errors = await _verify_single(_origin_metadata(), _consumer(), record=other)
    assert record is None
    assert errors and "another run" in errors[0]


@pytest.mark.asyncio
async def test_control_legacy_caller_with_consumer_task_keeps_its_purpose_compare():
    # verify_single_origin(task=...) is the legacy caller contract (code artifacts and the
    # legacy goal chain). It keeps the consuming compare, so the fix stays on the reuse paths.
    record, errors = await verify_single_origin(
        evidence_store=_EvidenceStore(_origin_record(_origin_metadata())),
        path_store=None,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        mission_id=MISSION,
        evidence_id=EVIDENCE,
        require_proposal=True,
        task=_consumer(),
        task_graph_store=_TaskStore(),
        work_store=_WorkStore(),
    )
    assert record is None
    assert errors and "purpose changed" in errors[0]


def _receipt(objective):
    return {
        "record_kind": TASK_RECEIPT_KIND,
        "task_id": TASK_B,
        "mission_id": MISSION,
        "run_id": RUN,
        "purpose_hash": purpose_hash(objective),
        "outcome": "success",
        "source_evidence_ids": [EVIDENCE],
        "related_attack_graph_node_ids": [],
        "related_hypothesis_ids": [],
    }


def test_control_consumer_receipt_binds_consumer_purpose():
    consumer = _consumer(OBJECTIVE_B)
    assert (
        verify_task_receipt(consumer, _receipt(OBJECTIVE_B), run_id=RUN, mission_id=MISSION) == []
    )
    assert verify_task_receipt(consumer, _receipt(OBJECTIVE_A), run_id=RUN, mission_id=MISSION) == [
        "task purpose changed since the accepted receipt",
    ]
