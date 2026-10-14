"""Diagnostic windows are readable DATA, never an execution proof accessor."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.diagnostics import diagnostic_for_exception
from redteam.orchestration.evidence_access import EvidenceReader, locate_raw, raw_from_metadata
from redteam.orchestration.local_tools import (
    LocalToolContext,
    LocalToolContractError,
    LocalToolDescriptor,
    LocalToolRegistry,
    LocalToolResult,
)
from tests.fakes.evidence_store import FakeEvidenceStore


def record(metadata, **overrides):
    return EvidenceRecord.model_validate({
        "evidence_id": "diag", "run_id": "run", "engagement_id": "eng",
        "source_type": EvidenceSourceType.SYSTEM, "source_id": "orchestration.runtime_tool",
        "content_type": "application/json", "digest": "a" * 64, "summary": "Local diagnostic",
        "created_at": datetime.now(timezone.utc), "metadata": metadata, **overrides,
    })


async def test_runtime_window_is_masked_scoped_bounded_and_never_raw():
    stored = record({"record_kind": "runtime_tool_result", "tool_id": "task.settle",
                     "result": {"ok": False, "payload": {
                         "api_key": "private-fixture-secret", "description": "x" * 10000,
                         "diagnostics": [{"code": "support_unproven"}],
                     }}})
    store = FakeEvidenceStore()
    await store.append(stored)
    reader = EvidenceReader(store)
    view = await reader.read("diag", "run", engagement_id="eng", limit=300)
    assert view["status"] == "ok" and view["view_kind"] == "runtime_diagnostic"
    assert view["execution_original"] is False and view["truncated"]
    assert len(view["text"]) == 300
    full = await reader.read("diag", "run", engagement_id="eng", limit=20000)
    assert "private-fixture-secret" not in full["text"]
    assert json.loads(full["text"])["result"]["ok"] is False
    assert raw_from_metadata(stored.metadata) is None
    assert locate_raw(stored, "run", "eng") == ("no_raw", None)
    for run, engagement in [("other", "eng"), ("run", "other")]:
        foreign = await reader.read("diag", run, engagement_id=engagement)
        assert foreign["status"] == "run_mismatch" and foreign["text"] == ""


@pytest.mark.parametrize("kind,source,view", [
    ("investigation_unresolved", "orchestration.investigation_result", "unresolved"),
    ("task_completion_receipt", "orchestration.task_completion_receipt", "review"),
    ("completion_review", "orchestration.task_completion_review", "review"),
    ("review_receipt", "registered-reviewer", "review"),
    ("runtime_tool_result", "unrecognized-producer", None),
])
async def test_owned_views_and_unsupported_records_remain_distinct(kind, source, view):
    stored = record({"record_kind": kind, "public": True, "stage": "task_completion_review",
                     "result": {"ok": False}}, source_id=source)
    store = FakeEvidenceStore()
    await store.append(stored)
    result = await EvidenceReader(store).read("diag", "run", engagement_id="eng")
    assert result["status"] == ("ok" if view else "no_raw")
    if view:
        assert result["view_kind"] == view and not result["execution_original"]
    else:
        assert result["reason"] == "unsupported_record_view"
    assert locate_raw(stored, "run")[0] == "no_raw"


async def test_input_repair_and_proven_handler_contract_defect_have_distinct_conditions():
    registry = LocalToolRegistry()

    async def invalid_result(arguments, context):
        return LocalToolResult(ok=False, runtime_outcome="task_completed")

    registry.register(LocalToolDescriptor(
        tool_id="owned.helper", description="Fixture handler",
        input_schema={"required": ["reference"]},
    ), invalid_result)
    context = LocalToolContext(deps=SimpleNamespace(), runtime=SimpleNamespace())
    bad_input = await registry.call("owned.helper", {}, context)
    diagnostic = bad_input.payload["diagnostic"]
    assert diagnostic["cause_scope"] == "model_input" and diagnostic["retryable"]
    with pytest.raises(LocalToolContractError) as caught:
        await registry.call("owned.helper", {"reference": "known"}, context)
    diagnostic = diagnostic_for_exception(caught.value, stage="native_local_tool")
    assert diagnostic.cause_scope == "runtime_contract" and not diagnostic.retryable
    assert diagnostic.owner == "orchestration.local_tools" and diagnostic.repair_condition


def test_result_retains_target_limitations_questions_and_source_identity():
    from redteam.core.contracts.mission import InvestigationResult, ResultQuestion
    from redteam.orchestration.central_results import result_for

    prior = InvestigationResult(
        result_id="stable-result", outcome="blocked", summary="Need target credentials",
        evidence_ids=["original"], contract_version=2,
        observations=["Response observed"], limitations=["Credential prerequisite remains unmet"],
        questions=[ResultQuestion(
            question_id="question", question="Can authorized access be provided?",
            evidence_ids=["original"], required_for_goal=True,
            reason="Access is needed for the goal",
        )],
    )
    task = SimpleNamespace(result=prior, task_id="task", investigation_contract_version=2,
                           related_hypothesis_ids=[])
    updated = result_for(task, outcome="inconclusive", summary="Returned independent observations",
                         evidence_ids=["independent-original"])
    assert updated.result_id == prior.result_id and updated.questions == prior.questions
    assert updated.observations == prior.observations and updated.limitations == prior.limitations
    assert updated.evidence_ids == ["original", "independent-original"]
