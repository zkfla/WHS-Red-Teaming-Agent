"""Actual-use receipts: null is explicit; absent and echoed values prove nothing."""

import pytest

from redteam.orchestration.analysis import _has_raw_account_artifact
from redteam.orchestration.session_proof import (
    actual_account_pairs,
    actual_receipt_conflict,
    collect_actual_accounts,
    collect_actual_sessions,
    has_anonymous_receipt,
    original_anonymous_receipt_matches,
)


@pytest.mark.parametrize("output", [{}, {"session_ref": None}, {"actual_session_ref": "s"},
                                    {"actual_session_ref": ""}, {"actual_session_ref": False}])
def test_missing_echo_named_or_malformed_is_not_anonymous(output):
    assert not has_anonymous_receipt({"anonymous": True, "principal_ref": "anonymous"},
                                     {"success": True, "output": output})


def test_explicit_original_null_is_anonymous_without_a_named_account():
    raw = {"success": True, "output": {"actual_session_ref": None}}
    assert has_anonymous_receipt({}, raw)
    assert not collect_actual_sessions({}, raw)
    assert not collect_actual_accounts({}, raw)


def test_top_level_null_is_anonymous_with_empty_custom_headers():
    raw = {"success": True, "output": {"actual_session_ref": None}}
    assert original_anonymous_receipt_matches({}, raw, {"request_headers": {}})


def test_custom_headers_make_top_level_anonymous_use_unproven():
    raw = {"success": True, "output": {"actual_session_ref": None}}
    assert not original_anonymous_receipt_matches(
        {}, raw, {"request_headers": {"x-api-key": "fixture-key"}}
    )


def test_nested_anonymous_receipt_remains_bound_to_its_own_unit():
    raw = {"success": True, "output": {
        "anonymous": {"actual_session_ref": None},
        "authenticated": {"actual_session_ref": "s-alice"},
    }}
    assert original_anonymous_receipt_matches(
        {}, raw, {"request_headers": {"x-api-key": "fixture-key"}}
    )


@pytest.mark.parametrize("metadata,raw_metadata,success", [
    ({"mock": True}, {}, True), ({}, {"fixture_only": True}, True), ({}, {}, False),
])
def test_failed_or_fixture_original_does_not_prove_anonymous(metadata, raw_metadata, success):
    assert not has_anonymous_receipt(metadata, {
        "success": success, "output": {"actual_session_ref": None}, "metadata": raw_metadata,
    })


@pytest.mark.parametrize("output,derived", [
    ({}, None), ({"actual_session_ref": "s"}, None), ({"actual_session_ref": None}, "s"),
    ({"actual_session_ref": []}, None),
])
def test_derived_receipt_cannot_invent_or_contradict_original(output, derived):
    metadata = {"http_observation": {"actual_session_ref": derived}}
    raw = {"success": True, "output": output}
    assert actual_receipt_conflict(metadata, raw)
    assert not has_anonymous_receipt(metadata, raw)


def test_comparison_proves_each_side_without_borrowing_unknown_side():
    raw = {"success": True, "output": {
        "anonymous": {"actual_session_ref": None},
        "authenticated": {"principal_ref": "alice", "session_ref": "s"},
    }}
    assert has_anonymous_receipt({}, raw)
    assert not collect_actual_accounts({}, raw)
    assert not _has_raw_account_artifact({}, raw, "alice", {"session_ref": "s"})
    raw["output"]["authenticated"]["actual_session_ref"] = "s"
    assert collect_actual_accounts({}, raw) == {"alice"}
    assert _has_raw_account_artifact({}, raw, "alice", {"session_ref": "s"})


def test_named_declaration_without_receipt_cannot_support_independent_claim():
    assert not _has_raw_account_artifact(
        {}, {"success": True, "output": {"principal_ref": "alice", "session_ref": "s"}},
        "alice", {"session_ref": "s"},
    )


def test_principal_never_borrows_another_units_requested_session():
    raw = {"success": True, "output": {
        "principal_ref": "alice", "actual_session_ref": "s-alice",
        "first_actual_session_ref": "s-bob",
        "first": {"principal_ref": "bob", "session_ref": "s-bob"},
    }}
    assert actual_account_pairs(raw) == {("s-alice", "alice")}
    assert not collect_actual_accounts({}, raw, {"s-bob"})
    derived = {"http_observation": {"actual_session_ref": "s-alice"},
               "session_comparison": {"first_actual_session_ref": "s-bob",
                                      "first_principal_ref": "bob"}}
    assert collect_actual_accounts(derived, raw) == {"alice"}


def test_pair_projection_matches_original_units_and_detects_null_conflicts():
    raw = {"success": True, "output": {
        "first": {"actual_session_ref": None}, "second": {"actual_session_ref": "s"},
    }}
    metadata = {"session_comparison": {
        "first_actual_session_ref": None, "second_actual_session_ref": "s",
    }}
    assert actual_receipt_conflict(metadata, raw) is None
    assert has_anonymous_receipt(metadata, raw)
    metadata["session_comparison"]["first_actual_session_ref"] = "s"
    assert actual_receipt_conflict(metadata, raw)
    assert not has_anonymous_receipt(metadata, raw)
