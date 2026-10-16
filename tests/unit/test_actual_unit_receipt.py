"""Strict original, unit-local session receipt validation."""

import pytest

from redteam.orchestration.session_proof import original_session_receipt_matches


@pytest.mark.parametrize(
    ("output", "expected", "matches"),
    [
        ({"actual_session_ref": "session-a"}, "session-a", True),
        ({}, "session-a", False),
        ({"session_ref": "session-a"}, "session-a", False),
        ({"actual_session_ref": None}, None, True),
        ({"actual_session_ref": None}, "session-a", False),
        ({"actual_session_ref": "  "}, "session-a", False),
        ({"actual_session_ref": 7}, "session-a", False),
    ],
)
def test_requires_valid_original_receipt_for_expected_value(output, expected, matches):
    assert original_session_receipt_matches(
        {}, {"success": True, "output": output}, expected_session_ref=expected
    ) is matches


@pytest.mark.parametrize(
    "output",
    [
        {"first_actual_session_ref": "session-a",
         "first": {"actual_session_ref": "session-b"}},
        {"first_actual_session_ref": "session-a",
         "first": {"actual_session_ref": None}},
    ],
)
def test_conflicting_flat_and_nested_receipts_fail(output):
    assert not original_session_receipt_matches(
        {}, {"success": True, "output": output}, unit="first",
        expected_session_ref="session-a",
    )


def test_derived_metadata_cannot_invent_or_conflict_with_original():
    raw = {"success": True, "output": {"actual_session_ref": "session-a"}}
    assert not original_session_receipt_matches(
        {"http_observation": {"actual_session_ref": "session-b"}}, raw,
        expected_session_ref="session-a",
    )
    assert not original_session_receipt_matches(
        {"http_observation": {"actual_session_ref": "session-a"}},
        {"success": True, "output": {}}, expected_session_ref="session-a",
    )


@pytest.mark.parametrize(
    "metadata,raw_metadata,success",
    [
        ({"mock": True}, {}, True),
        ({}, {"fixture_only": True}, True),
        ({}, {}, False),
    ],
)
def test_mock_fixture_or_failed_result_cannot_prove_receipt(
    metadata, raw_metadata, success
):
    assert not original_session_receipt_matches(
        metadata,
        {
            "success": success,
            "output": {"actual_session_ref": "session-a"},
            "metadata": raw_metadata,
        },
        expected_session_ref="session-a",
    )


def test_comparison_unit_does_not_borrow_receipt_from_other_unit():
    raw = {
        "success": True,
        "output": {
            "first_actual_session_ref": "session-a",
            "second_actual_session_ref": "session-b",
        },
    }
    assert original_session_receipt_matches(
        {}, raw, unit="first", expected_session_ref="session-a"
    )
    assert not original_session_receipt_matches(
        {}, raw, unit="second", expected_session_ref="session-a"
    )
    assert not original_session_receipt_matches(
        {}, raw, unit="request", expected_session_ref="session-a"
    )


@pytest.mark.parametrize("expected", ["", "  ", 4])
def test_malformed_expected_session_is_rejected(expected):
    assert not original_session_receipt_matches(
        {}, {"success": True, "output": {"actual_session_ref": "session-a"}},
        expected_session_ref=expected,
    )
