"""Native session selection uses registrations without weakening admission."""

from typing import Any

import pytest

from redteam.orchestration.knowledge_validation import (
    AssessmentError,
    InternalAssessment,
    check_principal_arguments,
    registered_principal_for_arguments,
)


def test_selected_session_pair_binds_registered_primary_and_checks_both() -> None:
    material = InternalAssessment(principal_bindings={
        "alice-principal": {"alice"}, "bob-principal": {"bob"},
    })
    arguments: dict[str, Any] = {"first_session_ref": "alice", "second_session_ref": "bob"}
    principal = registered_principal_for_arguments(arguments, material)
    assert principal == "alice-principal"
    check_principal_arguments(None, principal, arguments, material)
    with pytest.raises(AssessmentError):
        check_principal_arguments(
            None, principal, {**arguments, "second_session_ref": "unregistered"}, material,
        )


def test_ambiguous_session_owner_is_not_chosen() -> None:
    material = InternalAssessment(principal_bindings={"first": {"shared"}, "second": {"shared"}})
    arguments: dict[str, Any] = {"session_ref": "shared"}
    principal = registered_principal_for_arguments(arguments, material)
    assert principal is None
    with pytest.raises(AssessmentError):
        check_principal_arguments(None, principal, arguments, material)


def test_registered_session_alias_does_not_require_guessing_underlying_owner() -> None:
    material = InternalAssessment(principal_bindings={
        "alice": {"alice"}, "principal:alice": {"alice"}, "credential:alice": {"alice"},
    })
    arguments: dict[str, Any] = {"session_ref": "alice"}
    principal = registered_principal_for_arguments(arguments, material)
    assert principal == "alice"
    check_principal_arguments(None, principal, arguments, material)


def test_changed_registration_still_rejects_bound_identity_at_admission() -> None:
    arguments: dict[str, Any] = {"session_ref": "alice"}
    principal = registered_principal_for_arguments(
        arguments, InternalAssessment(principal_bindings={"alice-principal": {"alice"}}),
    )
    with pytest.raises(AssessmentError):
        check_principal_arguments(None, principal, arguments, InternalAssessment())
