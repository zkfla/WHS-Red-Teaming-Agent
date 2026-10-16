from __future__ import annotations

from typing import Any

from redteam.orchestration.completion import (
    _completion_has_anonymous_receipt,
    _verified_completion_accounts,
)


def _proof(
    output: dict[str, Any],
    arguments: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    success: bool = True,
    claims: set[str] | None = None,
    required: set[str] | None = None,
) -> set[str]:
    raw = {"success": success, "output": output}
    return _verified_completion_accounts(
        metadata or {}, raw, claims or {"alice", "bob"}, arguments or {
            "first_session_ref": "s-alice", "second_session_ref": "s-bob",
        }, required or claims or {"alice", "bob"},
    )


def test_matching_flat_comparison_units_prove_both_accounts_and_aliases():
    assert _proof({
        "first_actual_session_ref": "s-alice",
        "first_principal_ref": "alice",
        "second_actual_session_ref": "s-bob",
        "second_principal_ref": "bob",
    }) == {"alice", "bob"}
    assert _proof({
        "first_actual_session_ref": "s-alice", "first_principal_ref": "alice",
        "second_actual_session_ref": "s-bob", "second_principal_ref": "bob",
    }, claims={"s-alice", "s-bob"}, required={"s-alice", "s-bob"}) == {
        "s-alice", "s-bob",
    }


def test_matching_nested_comparison_units_prove_both_accounts_and_aliases():
    assert _proof({
        "first": {"actual_session_ref": "s-alice", "principal_ref": "alice"},
        "second": {"actual_session_ref": "s-bob", "principal_ref": "bob"},
    }) == {"alice", "bob"}


def test_swapped_comparison_receipts_cover_neither_requested_leg():
    assert _proof({
        "first_actual_session_ref": "s-bob", "first_principal_ref": "bob",
        "second_actual_session_ref": "s-alice", "second_principal_ref": "alice",
    }) == set()


def test_mismatched_and_partial_receipts_cover_only_the_matching_leg():
    assert _proof({
        "first_actual_session_ref": "other", "first_principal_ref": "alice",
        "second_actual_session_ref": "s-bob", "second_principal_ref": "bob",
    }) == {"bob"}
    assert _proof({
        "first_actual_session_ref": "s-alice", "first_principal_ref": "alice",
    }) == {"alice"}


def test_account_on_mismatched_unit_cannot_borrow_another_units_same_session():
    assert _proof({
        "first_actual_session_ref": "s-bob",
        "first_principal_ref": "eve",
        "second_actual_session_ref": "s-bob",
        "second_principal_ref": "bob",
    }) == {"bob"}


def test_missing_receipts_do_not_cover_claims():
    assert _proof({"first_principal_ref": "alice", "second_principal_ref": "bob"}) == set()


def test_derived_receipt_or_mock_raw_cannot_cover_requested_units():
    output = {
        "first_principal_ref": "alice", "second_principal_ref": "bob",
    }
    derived = {"session_comparison": {
        "first_actual_session_ref": "s-alice",
        "second_actual_session_ref": "s-bob",
    }}
    assert _proof(output, metadata=derived) == set()
    assert _proof(
        {
            "first_actual_session_ref": "s-alice", "first_principal_ref": "alice",
            "second_actual_session_ref": "s-bob", "second_principal_ref": "bob",
        },
        metadata={"mock": True},
    ) == set()


def test_missing_expected_leg_cannot_borrow_the_other_leg_receipt():
    assert _proof(
        {"first_actual_session_ref": "s-alice", "first_principal_ref": "alice"},
        {"first_session_ref": "s-alice"},
    ) == {"alice"}


def test_explicit_anonymous_null_is_not_a_named_account_or_session():
    assert _proof({
        "first_actual_session_ref": None,
        "second_actual_session_ref": "s-bob", "second_principal_ref": "bob",
    }) == {"bob"}


def test_anonymous_completion_receipt_must_belong_to_anonymous_or_request_unit():
    assert _completion_has_anonymous_receipt({}, {
        "success": True,
        "output": {
            "anonymous": {"actual_session_ref": None},
            "authenticated": {"actual_session_ref": "s-alice"},
        },
    }, {"session_ref": "s-alice"})
    assert not _completion_has_anonymous_receipt({}, {
        "success": True,
        "output": {"first_actual_session_ref": None},
    }, {"first_session_ref": "s-alice"})
    assert _completion_has_anonymous_receipt({}, {
        "success": True,
        "output": {"actual_session_ref": None},
    }, {})
