"""Account claims may use either leg of a measured session comparison."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from redteam.core.enums import EvidenceSourceType
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.analysis import (
    StructuredLLMAnalysisVerifier,
    _has_raw_account_artifact,
)
from redteam.orchestration.cognition import DecisionBudget


def comparison_fixture(*, success: bool = True):
    raw = {
        "success": success,
        "action_id": "action-1",
        "invocation_id": "invocation-1",
        "output": {
            "first": {"actual_session_ref": "alice", "principal_ref": "test-alice"},
            "second": {"actual_session_ref": "bob", "principal_ref": "test-bob"},
        },
    }
    metadata = {
        "session_comparison": {
            "first_session_ref": "alice",
            "first_principal_ref": "test-alice",
            "second_session_ref": "bob",
            "second_principal_ref": "test-bob",
        }
    }
    arguments = {"first_session_ref": "alice", "second_session_ref": "bob"}
    return metadata, raw, arguments


def test_selected_account_can_be_proven_by_either_actual_comparison_leg():
    metadata, raw, arguments = comparison_fixture()

    assert _has_raw_account_artifact(metadata, raw, "test-bob", arguments)
    assert _has_raw_account_artifact(metadata, raw, "test-alice", arguments)
    # The observed second actual session is also its supported short alias.
    assert _has_raw_account_artifact(metadata, raw, "bob", arguments)


def test_unrelated_valid_leg_does_not_invalidate_single_account_claim():
    metadata, raw, arguments = comparison_fixture()

    assert _has_raw_account_artifact(metadata, raw, "test-bob", arguments)


def test_actual_principal_cannot_be_borrowed_from_the_other_comparison_leg():
    metadata, raw, arguments = comparison_fixture()
    metadata["session_comparison"]["second_principal_ref"] = "test-alice"

    assert not _has_raw_account_artifact(metadata, raw, "test-alice", arguments)
    assert not _has_raw_account_artifact(metadata, raw, "test-bob", arguments)


def test_flat_and_nested_original_principals_for_one_side_must_agree():
    metadata, raw, arguments = comparison_fixture()
    raw["output"]["second_actual_session_ref"] = "bob"
    raw["output"]["second_principal_ref"] = "test-alice"

    assert not _has_raw_account_artifact(metadata, raw, "test-bob", arguments)


@pytest.mark.parametrize("case", [
    "derived_spoof",
    "inventory_only",
    "actual_session_null",
    "selected_session_mismatch",
    "failed_raw",
])
def test_non_actual_or_inconsistent_comparison_does_not_prove_claim(case: str):
    metadata, raw, arguments = comparison_fixture(success=case != "failed_raw")
    if case == "derived_spoof":
        metadata["session_comparison"]["second_principal_ref"] = "test-alice"
    elif case == "inventory_only":
        raw["output"] = {"sessions": [
            {"target_ref": "target", "session_ref": "bob", "principal_ref": "test-bob"}
        ]}
        metadata["session_comparison"] = {
            "first_session_ref": "alice", "second_session_ref": "bob",
            "first_principal_ref": "test-alice", "second_principal_ref": "test-bob",
        }
    elif case == "actual_session_null":
        raw["output"]["second"]["actual_session_ref"] = None
    elif case == "selected_session_mismatch":
        arguments["second_session_ref"] = "other-bob"

    assert not _has_raw_account_artifact(metadata, raw, "test-bob", arguments)


class _EvidenceStore:
    def __init__(self, record):
        self.record = record

    async def get(self, _evidence_id):
        return self.record


class _FullEvidenceStore:
    def __init__(self, records):
        self.records = records

    async def get(self, evidence_id):
        return next(
            (record for record in self.records if str(record.evidence_id) == str(evidence_id)),
            None,
        )

    async def list_for_run(self, run_id):
        return [record for record in self.records if str(record.run_id) == str(run_id)]


def _execution_record(
    *,
    evidence_id: str,
    raw: dict[str, Any],
    arguments: dict[str, Any],
    proposal_target_ref: str = "target-node",
    metadata_target_node_id: str = "target-node",
):
    action_id = raw["action_id"]
    invocation_id = raw["invocation_id"]
    metadata = {
        "tool_result": raw,
        "target_node_id": metadata_target_node_id,
        "action_id": action_id,
        "invocation_id": invocation_id,
        "tool_id": "tool-1",
        "proposal": {
            "action_id": action_id,
            "run_id": "run-1",
            "engagement_id": "engagement-1",
            "agent_id": "agent-1",
            "target": {"kind": "graph_node", "node_id": proposal_target_ref},
            "reason": "Test evidence origin.",
            "created_at": "2026-10-16T00:00:00Z",
            "arguments": arguments,
        },
    }
    return SimpleNamespace(
        evidence_id=evidence_id,
        run_id="run-1",
        engagement_id="engagement-1",
        source_type=EvidenceSourceType.TOOL,
        source_id="tool-1",
        action_id=action_id,
        invocation_id=invocation_id,
        metadata=metadata,
    )


def _comparison_alias_records(case: str | None = None):
    metadata, raw, arguments = comparison_fixture()
    raw["tool_id"] = "tool-1"
    raw["output"]["second"]["actual_session_ref"] = "session-bob"
    metadata["session_comparison"]["second_session_ref"] = "session-bob"
    metadata["session_comparison"]["second_actual_session_ref"] = "session-bob"
    arguments.update({"second_session_ref": "session-bob"})
    if case == "swapped_leg":
        raw["output"]["first"]["actual_session_ref"] = "session-bob"
    elif case == "missing_comparison_metadata":
        raw["output"]["first"]["actual_session_ref"] = "session-bob"
    elif case == "mismatched_session":
        arguments["second_session_ref"] = "another-session"
    elif case == "derived_spoof":
        metadata["session_comparison"]["second_principal_ref"] = "test-alice"
    cited = _execution_record(evidence_id="evidence-1", raw=raw, arguments=arguments)
    cited.metadata["session_comparison"] = metadata["session_comparison"]
    if case == "missing_comparison_metadata":
        cited.metadata.pop("session_comparison")

    alias_raw = {
        "success": True,
        "action_id": "action-alias",
        "invocation_id": "invocation-alias",
        "tool_id": "tool-1",
        "output": {"actual_session_ref": "session-bob", "principal_ref": "bob"},
    }
    alias = _execution_record(
        evidence_id="evidence-alias",
        raw=alias_raw,
        arguments={},
    )
    return cited, alias


@pytest.mark.parametrize("claim_principal,alias_ok,expected_called", [
    ("test-bob", False, True),
    ("bob", True, True),
    ("bob", False, False),
])
async def test_posthoc_account_proof_gates_meaning_review(
    monkeypatch, claim_principal: str, alias_ok: bool, expected_called: bool
):
    metadata, raw, arguments = comparison_fixture()
    if claim_principal == "bob":
        # Exercise the separate measured-session alias fallback with a
        # session name that cannot satisfy the direct session-alias check.
        raw["output"]["second"]["actual_session_ref"] = "session-bob"
        metadata["session_comparison"]["second_session_ref"] = "session-bob"
        arguments["second_session_ref"] = "session-bob"
        metadata["session_comparison"]["second_principal_ref"] = "test-bob"
    raw["metadata"] = {"success": True}
    metadata.update({
        "tool_result": raw,
        "target_node_id": "target-node",
        "proposal": {"arguments": arguments},
    })
    record = SimpleNamespace(
        run_id="run-1",
        engagement_id="engagement-1",
        action_id="action-1",
        invocation_id="invocation-1",
        metadata=metadata,
    )
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _EvidenceStore(record))
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis._posthoc_source_gap", lambda *_: None
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis.claim_target_is_grounded",
        AsyncMock(return_value=True),
    )
    alias_calls = []

    async def alias_grounded(**kwargs):
        alias_calls.append(kwargs)
        return alias_ok and kwargs.get("session_name") == "session-bob"

    monkeypatch.setattr(
        "redteam.orchestration.analysis._session_alias_grounded", alias_grounded
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "Measured response.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": [claim_principal]},
        evidence_ids=["evidence-1"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert bool(meaning_call.await_count) is expected_called
    if claim_principal == "bob" and expected_called:
        assert any(call["session_name"] == "session-bob" for call in alias_calls)
    if expected_called:
        assert assessment.outcome == "success"
    else:
        assert assessment.reason.startswith("claimed account lacks actual-use proof")


@pytest.mark.parametrize("case", [
    "swapped_leg", "mismatched_session", "derived_spoof",
    "missing_comparison_metadata",
])
async def test_invalid_comparison_source_cannot_borrow_runwide_alias_proof(
    monkeypatch, case: str
):
    cited, alias = _comparison_alias_records(case)
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _FullEvidenceStore([cited, alias]))
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis._posthoc_source_gap", lambda *_: None
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis.claim_target_is_grounded",
        AsyncMock(return_value=True),
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "Measured response.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": ["bob"]},
        evidence_ids=["evidence-1"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert meaning_call.await_count == 0
    assert assessment.reason.startswith("comparison account receipt is inconsistent")


async def test_consistent_comparison_can_use_real_runwide_alias_proof(monkeypatch):
    cited, alias = _comparison_alias_records()
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _FullEvidenceStore([cited, alias]))
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis._posthoc_source_gap", lambda *_: None
    )
    monkeypatch.setattr(
        "redteam.orchestration.analysis.claim_target_is_grounded",
        AsyncMock(return_value=True),
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "Measured response.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": ["bob"]},
        evidence_ids=["evidence-1"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert meaning_call.await_count == 1
    assert assessment.outcome == "success"


@pytest.mark.parametrize(
    "proposal_target_ref,expected_called",
    [("other-target", False), ("target-node", True)],
)
async def test_alias_candidate_must_match_its_original_proposal_target(
    monkeypatch, proposal_target_ref: str, expected_called: bool
):
    cited, alias = _comparison_alias_records()
    alias.metadata["proposal"]["target"] = {
        "kind": "graph_node",
        "node_id": proposal_target_ref,
    }
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _FullEvidenceStore([cited, alias]))
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "Measured response.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": ["bob"]},
        evidence_ids=["evidence-1"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert bool(meaning_call.await_count) is expected_called
    assert "target_ref" not in cited.metadata["proposal"]["arguments"]
    if expected_called:
        assert assessment.outcome == "success"
    else:
        assert assessment.reason.startswith("claimed account lacks actual-use proof")


@pytest.mark.parametrize("receipt", [
    {"anonymous": {"actual_session_ref": None},
     "authenticated": {"actual_session_ref": "session-alice", "principal_ref": "alice"}},
    {"actual_session_ref": None},
])
async def test_posthoc_anonymous_claim_accepts_only_an_explicit_anonymous_unit(
    monkeypatch, receipt: dict[str, Any]
):
    raw = {
        "success": True,
        "action_id": "action-1",
        "invocation_id": "invocation-1",
        "tool_id": "tool-1",
        "output": receipt,
    }
    record = _execution_record(evidence_id="evidence-anonymous", raw=raw, arguments={})
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _EvidenceStore(record))
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "The original anonymous request measured the target.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": ["anonymous"]},
        evidence_ids=["evidence-anonymous"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert meaning_call.await_count == 1
    assert assessment.outcome == "success"


@pytest.mark.parametrize("case", [
    "absent",
    "wrong_comparison_leg",
    "named_request_null",
    "failed",
    "mock",
    "derived_only",
])
async def test_posthoc_anonymous_claim_rejects_missing_or_untrusted_receipts(
    monkeypatch, case: str
):
    arguments: dict[str, Any] = {}
    raw = {
        "success": case != "failed",
        "action_id": "action-1",
        "invocation_id": "invocation-1",
        "tool_id": "tool-1",
        "output": {},
    }
    if case == "wrong_comparison_leg":
        raw["output"] = {
            "first_actual_session_ref": None,
            "second_actual_session_ref": "session-alice",
            "second_principal_ref": "alice",
        }
        arguments = {"first_session_ref": "session-first", "second_session_ref": "session-alice"}
    elif case == "named_request_null":
        raw["output"] = {"actual_session_ref": None}
        arguments = {"session_ref": "session-alice"}
    elif case in {"failed", "mock"}:
        raw["output"] = {"anonymous": {"actual_session_ref": None}}
        if case == "mock":
            raw["metadata"] = {"mock": True}
    elif case == "derived_only":
        record_metadata = {"http_observation": {"actual_session_ref": None}}
    else:
        record_metadata = None

    record = _execution_record(evidence_id="evidence-anonymous", raw=raw, arguments=arguments)
    if case == "derived_only":
        record.metadata.update(record_metadata)
    verifier = StructuredLLMAnalysisVerifier(
        object(), cast(EvidenceStore, _EvidenceStore(record))
    )
    meaning_call = AsyncMock(return_value={
        "meaning": "The evidence supports the claim.",
        "outcome": "success",
        "reason": "Should not be reached without an original anonymous receipt.",
        "claim_supported": True,
    })
    monkeypatch.setattr(verifier._decider, "ask", cast(Any, meaning_call))

    assessment = await verifier.verify_claim_with_budget(
        claim={"target_node_id": "target-node", "principal_refs": ["anonymous"]},
        evidence_ids=["evidence-anonymous"],
        run_id="run-1",
        budget=DecisionBudget(1),
    )

    assert meaning_call.await_count == 0
    assert assessment.outcome == "unknown"
