"""Account claims may use either leg of a measured session comparison."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

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
