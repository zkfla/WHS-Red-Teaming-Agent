"""Candidate review support is a bounded navigation view, never a proof or read grant."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.contracts.mission import (
    InvestigationResult,
    MissionPlan,
    MissionTask,
    Provenance,
)
from redteam.core.enums import EvidenceSourceType, TaskStatus
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.planner_context import (
    PlannerPassContext,
    PlannerReviewedReferenceError,
)

RUN = "run-review-support"
ENGAGEMENT = "eng-review-support"
MISSION = "mission-review-support"
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def _task(identity, *, updated_at, related=(), revisions=None, result=None,
          source_task_id=None, source_result_id=None, source_question_id=None,
          evidence_ids=()):
    return MissionTask(
        task_id=identity,
        mission_id=MISSION,
        title=f"Task {identity}",
        objective=f"Inspect {identity}",
        status=TaskStatus.COMPLETED,
        required=False,
        investigation_contract_version=1,
        related_hypothesis_ids=list(related),
        hypothesis_revisions=dict(revisions or {}),
        result=result,
        source_task_id=source_task_id,
        source_result_id=source_result_id,
        source_question_id=source_question_id,
        evidence_ids=list(evidence_ids),
        provenance=Provenance(source="fixture"),
        created_at=updated_at,
        updated_at=updated_at,
    )


def _original(identity, task_id, created_at):
    metadata = {
        "mission_task_id": task_id,
        "tool_result": {"success": True, "output": {"fixture": identity}},
    }
    return EvidenceRecord(
        evidence_id=identity,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source_type=EvidenceSourceType.TOOL,
        source_id="fixture-tool",
        content_type="application/json",
        digest=content_hash(metadata),
        created_at=created_at,
        metadata=metadata,
    )


def _receipt(identity, candidate, *, public=True, revision=None, created_at=NOW):
    metadata = {
        "record_kind": "review_receipt",
        "public": public,
        "admission": "final",
        "hypothesis_id": candidate.hypothesis_id,
        "candidate_revision": candidate.revision if revision is None else revision,
        "candidate_purpose_hash": content_hash(hypothesis_purpose(candidate)),
        "hypothesis_outcome": "unknown",
        "private_reasoning": "Fixture-only and never projected",
    }
    return EvidenceRecord(
        evidence_id=identity,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source_type=EvidenceSourceType.SYSTEM,
        source_id="fixture-review",
        content_type="application/json",
        digest=content_hash(metadata),
        created_at=created_at,
        metadata=metadata,
    )


def _context(tasks, candidate, records, *, applications=(), promotions=(), findings=()):
    mission = MissionPlan(
        mission_id=MISSION,
        engagement_id=ENGAGEMENT,
        objective="Review a bounded candidate",
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="fixture"),
    )
    canonical = SimpleNamespace(
        mission=mission,
        binding="b" * 64,
        tasks={str(item.task_id): item for item in tasks},
        candidates={str(candidate.hypothesis_id): candidate},
        executions=[],
        nodes={},
        edges=[],
        graph_roots=(),
        graph_truncated=False,
        review_applications=tuple(applications),
        finding_promotions=tuple(promotions),
        findings=tuple(findings),
    )
    validity = {"evidence_validity": []}
    return PlannerPassContext(
        canonical,
        records,
        validity,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        evidence_store=object(),
        graph=None,
    )


def test_current_revision_task_and_source_original_are_joined_without_becoming_reads():
    candidate = Hypothesis(
        contract_version=2,
        hypothesis_id="hyp-current",
        description="A bounded premise requiring independent review",
        status="open",
        revision=2,
        target_node_ids=["node-a"],
        reason="Fixture candidate",
    )
    root_result = InvestigationResult(
        result_id="result-root",
        outcome="inconclusive",
        summary="The source observation is retained",
        evidence_ids=["original-root"],
    )
    grand_result = InvestigationResult(
        result_id="result-grand",
        outcome="inconclusive",
        summary="An older source result remains behind the one-hop bound",
        evidence_ids=["original-grand"],
    )
    grand = _task("task-grand", updated_at=NOW - timedelta(days=3), result=grand_result,
                  evidence_ids=["original-grand"])
    root = _task("task-root", updated_at=NOW - timedelta(days=2), result=root_result,
                 source_task_id=grand.task_id, source_result_id=grand_result.result_id,
                 source_question_id="question-grand", evidence_ids=["original-root"])
    child_result = InvestigationResult(
        result_id="result-child",
        outcome="confirmed",
        summary="The Task purpose was confirmed",
    )
    child = _task(
        "task-candidate",
        updated_at=NOW - timedelta(hours=1),
        related=[candidate.hypothesis_id],
        revisions={candidate.hypothesis_id: candidate.revision},
        result=child_result,
        source_task_id=root.task_id,
        source_result_id=root_result.result_id,
        source_question_id="question-root",
    )
    unrelated = [
        _task(f"task-focus-{index}", updated_at=NOW - timedelta(minutes=index + 1))
        for index in range(3)
    ]
    tasks = [grand, root, child, *unrelated]

    # Ten newer captured originals fill both bounded windows. Only four recent Task IDs
    # become overview feedback, so the source Task is omitted from the hydrated frontier.
    records = [
        _original("original-grand", str(grand.task_id), NOW - timedelta(days=3)),
        _original("original-root", str(root.task_id), NOW - timedelta(days=2)),
    ]
    owners = [str(child.task_id), *(str(item.task_id) for item in unrelated)]
    for index in range(10):
        records.append(_original(
            f"original-recent-{index}", owners[index % len(owners)],
            NOW - timedelta(minutes=10 - index),
        ))
    records.extend([
        _receipt("receipt-current", candidate),
        _receipt("receipt-stale", candidate, revision=1),
        _receipt("receipt-private", candidate, public=False),
    ])

    context = _context(tasks, candidate, records)
    fields = context.base_fields()

    assert str(root.task_id) not in {item["task_id"] for item in fields["investigations"]}
    assert "original-root" not in {item["evidence_id"]
                                   for item in fields["original_observations"]["records"]}
    support, = [item for item in fields["candidate_review_support"]
                if item["candidate_id"] == candidate.hypothesis_id]
    assert support["candidate_revision"] == candidate.revision
    assert support["candidate_purpose_hash"] == content_hash(hypothesis_purpose(candidate))
    assert support["current_public_receipt_ids"] == ["receipt-current"]
    linked, = [item for item in support["linked_tasks"]
               if item["task_id"] == child.task_id]
    assert linked["task_revision"] == candidate.revision
    assert linked["result_id"] == child_result.result_id
    assert linked["outcome"] == child_result.outcome
    assert linked["source_link"] == {
        "task_id": root.task_id,
        "expected_result_id": root_result.result_id,
        "result_id": root_result.result_id,
        "link_status": "matched",
    }
    assert linked["captured_original_ids"] == ["original-root"]
    assert linked["source_chain_truncated"] is True
    assert linked["omitted_source_chain_minimum_count"] == 1
    assert "receipt-stale" not in json_receipt_ids(fields)
    assert "receipt-private" not in json_receipt_ids(fields)
    assert "private_reasoning" not in str(fields["review_receipts"])

    # The summary is only navigation data: citing an unhydrated source Task as reviewed
    # still requires an actual context lookup/read in this Planner pass.
    with pytest.raises(PlannerReviewedReferenceError):
        context.validate_reviewed(SimpleNamespace(
            reviewed_task_ids=[str(root.task_id)], reviewed_hypothesis_ids=[]))


def json_receipt_ids(fields):
    return {item["evidence_id"] for item in fields["review_receipts"]}


def test_same_revision_filter_mismatch_refusal_and_link_bounds():
    candidate = Hypothesis(
        contract_version=2,
        hypothesis_id="hyp-current",
        description="A bounded premise requiring independent review",
        status="open",
        revision=2,
        target_node_ids=["node-a"],
        reason="Fixture candidate",
    )
    mismatch_result = InvestigationResult(
        result_id="actual-result",
        outcome="inconclusive",
        summary="Source purpose did not match",
        evidence_ids=["original-mismatch"],
    )
    source = _task("task-source", updated_at=NOW - timedelta(days=1),
                   result=mismatch_result, evidence_ids=["original-mismatch"])
    linked = [
        _task(
            f"task-current-{index}",
            updated_at=NOW - timedelta(minutes=index + 1),
            related=[candidate.hypothesis_id],
            revisions={candidate.hypothesis_id: candidate.revision},
            result=InvestigationResult(
                result_id=f"result-current-{index}",
                outcome="inconclusive",
                summary="Current purpose result",
            ),
            **({"source_task_id": str(source.task_id),
                "source_result_id": "a-different-result",
                "source_question_id": "source-question"} if index == 0 else {}),
        )
        for index in range(5)
    ]
    stale = _task(
        "task-stale-revision",
        updated_at=NOW,
        related=[candidate.hypothesis_id],
        revisions={candidate.hypothesis_id: 1},
        result=InvestigationResult(
            result_id="result-stale",
            outcome="confirmed",
            summary="Older candidate revision",
        ),
    )
    records = [
        _original("original-mismatch", str(source.task_id), NOW - timedelta(days=1)),
        *(_original(f"current-original-{index}", str(task.task_id), task.updated_at)
          for index, task in enumerate(linked)),
        _receipt("receipt-current", candidate),
    ]
    context = _context([source, *linked, stale], candidate, records)

    fields = context.base_fields()
    support, = [item for item in fields["candidate_review_support"]
                if item["candidate_id"] == candidate.hypothesis_id]
    assert support["linked_task_count"] == 5
    assert support["omitted_linked_task_count"] == 1
    assert len(support["linked_tasks"]) == 4
    assert "task-stale-revision" not in {
        item["task_id"] for item in support["linked_tasks"]}
    mismatched, = [item for item in support["linked_tasks"]
                   if item["task_id"] == "task-current-0"]
    assert mismatched["source_link"] == {
        "task_id": str(source.task_id),
        "result_id": mismatch_result.result_id,
        "expected_result_id": "a-different-result",
        "link_status": "result_mismatch",
    }
    assert mismatched["captured_original_ids"] == []
    assert mismatched["source_chain_truncated"] is False
    assert support["current_public_receipt_ids"] == ["receipt-current"]


def test_fully_visible_current_revision_context_does_not_duplicate_support():
    candidate = Hypothesis(
        contract_version=2,
        hypothesis_id="hyp-visible",
        description="A fully visible premise",
        status="open",
        revision=1,
        target_node_ids=["node-a"],
        reason="Fixture candidate",
    )
    original = _original("original-visible", "task-visible", NOW)
    task = _task(
        "task-visible",
        updated_at=NOW,
        related=[candidate.hypothesis_id],
        revisions={candidate.hypothesis_id: candidate.revision},
        result=InvestigationResult(
            result_id="result-visible",
            outcome="inconclusive",
            summary="The purpose result is present",
            evidence_ids=[str(original.evidence_id)],
        ),
    )
    context = _context([task], candidate, [original])

    fields = context.base_fields()

    assert fields["investigations"]
    assert fields["original_observations"]["records"]
    assert fields["candidate_review_support"] == []
    assert fields["candidate_review_support_omitted_count"] == 0


def test_applied_review_projects_canonical_candidate_finding_and_limitations():
    candidate = Hypothesis(
        contract_version=2,
        hypothesis_id="hyp-confirmed",
        description="A bounded authentication bypass",
        status="confirmed",
        revision=2,
        target_node_ids=["node-a"],
        reason="Independent review supported the bounded claim",
    )
    finding = Finding(
        finding_id="find-confirmed",
        title="Authentication bypass",
        severity="high",
        verified=True,
        evidence_ids=["original-a", "receipt-a"],
        metadata={"reviewed_finding_detail": {
            "limitations": ["Only the retained endpoint and principal were reviewed."],
        }},
    )
    application = {
        "review_receipt_id": "receipt-a",
        "candidate_id": candidate.hypothesis_id,
        "candidate_pre_verdict_revision": 1,
        "candidate_revision": 2,
        "finding_id": finding.finding_id,
        "status": "promoted",
        "reason": "",
        "review_receipt_hash": "not-for-the-planner",
    }
    promotion = {
        "candidate_id": candidate.hypothesis_id,
        "candidate_revision": 2,
        "finding_id": finding.finding_id,
        "status": "promoted",
    }
    fields = _context(
        [], candidate, [], applications=[application], promotions=[promotion], findings=[finding],
    ).base_fields()

    state = fields["candidate_review_state"]
    record, = state["records"]
    assert record["application_status"] == "promoted"
    assert record["canonical_candidate_status"] == "confirmed"
    assert record["promotion_status"] == "promoted"
    assert record["finding"]["finding_id"] == finding.finding_id
    assert record["finding"]["limitations"] == [
        "Only the retained endpoint and principal were reviewed.",
    ]
    assert "review_receipt_hash" not in str(state)


def test_pending_review_application_is_navigation_state_without_a_verdict():
    candidate = Hypothesis(
        contract_version=2,
        hypothesis_id="hyp-pending",
        description="A bounded claim awaiting canonical settlement",
        status="open",
        revision=1,
        target_node_ids=["node-a"],
        reason="The reviewed draft has not reached its Task result",
    )
    fields = _context([], candidate, [], applications=[{
        "review_receipt_id": "receipt-pending",
        "candidate_id": candidate.hypothesis_id,
        "candidate_pre_verdict_revision": 1,
        "candidate_revision": None,
        "finding_id": None,
        "status": "pending_task_result",
        "reason": "reviewed assessment draft awaits canonical task settlement",
    }]).base_fields()

    record, = fields["candidate_review_state"]["records"]
    assert record["application_status"] == "pending_task_result"
    assert record["canonical_candidate_status"] == "open"
    assert record["finding"] is None and record["promotion_status"] is None
