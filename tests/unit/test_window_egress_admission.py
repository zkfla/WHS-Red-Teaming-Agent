"""TASK-004A: window egress-admission helper (pure, UNWIRED).

An accepted admission is metering plus a destination attempt: never
contact, never a reached peer, never an application response, never
remote content, never session provenance. These tests pin the renamed
semantics and the holder-binding honesty rule. Nothing here is consumed
by _write_fact; remote-subject promotion stays on HOLD per root.
"""

from __future__ import annotations

from typing import Any

from redteam.orchestration.observations.window_egress_admission import (
    destination_attempts,
    detect_companion_conflict,
    validate_admission_companion,
)


def _companion(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "window_id": "win-1",
        "run_id": "run-1",
        "instance_id": "inst-1",
        "epoch": 1,
        "holder": {"action_id": "action-1", "invocation_id": "inv-1"},
        "scope": [{"ip": "10.0.0.5", "port": 443, "proto": "tcp"}],
        "admissions": [
            {
                "window_id": "win-1",
                "run_id": "run-1",
                "instance_id": "inst-1",
                "epoch": 1,
                "daemon_session_id": "sess-1",
                "seq": 0,
                "flow_key": "tcp:10.0.0.5:443",
                "dst_ip": "10.0.0.5",
                "dst_port": 443,
                "verdict": "accepted",
                "observed_hook": "flow_init_queue",
            }
        ],
    }
    base.update(overrides)
    return base


def test_valid_companion_with_holder_passes():
    assert validate_admission_companion(_companion()) == []


def test_empty_admissions_is_valid_metering():
    assert validate_admission_companion(_companion(admissions=[])) == []
    assert destination_attempts(_companion(admissions=[])) == []


def test_missing_holder_is_metering_only():
    companion = _companion()
    del companion["holder"]
    errors = validate_admission_companion(companion)
    assert any("holder binding required" in item for item in errors)


def test_blank_holder_ids_are_rejected():
    companion = _companion(holder={"action_id": " ", "invocation_id": "inv-1"})
    errors = validate_admission_companion(companion)
    assert any("holder binding required" in item for item in errors)


def test_admission_window_mismatch_is_rejected():
    companion = _companion()
    companion["admissions"][0]["window_id"] = "win-2"
    errors = validate_admission_companion(companion)
    assert any("must equal the companion window_id" in item for item in errors)


def test_missing_hook_is_rejected():
    companion = _companion()
    del companion["admissions"][0]["observed_hook"]
    errors = validate_admission_companion(companion)
    assert any("observed_hook" in item for item in errors)


def test_bad_scope_port_is_rejected():
    errors = validate_admission_companion(_companion(scope=[{"ip": "x", "port": 0}]))
    assert any("scope[0].port" in item for item in errors)


def test_attempts_project_without_contact_language():
    attempts = destination_attempts(_companion())
    assert attempts == [
        {
            "dst_ip": "10.0.0.5",
            "dst_port": 443,
            "verdict": "accepted",
            "observed_hook": "flow_init_queue",
        }
    ]
    assert "contact" not in str(attempts)


def test_conflicting_companions_are_detected():
    first = _companion()
    second = _companion()
    second["admissions"][0]["verdict"] = "dropped"
    token = detect_companion_conflict(first, second)
    assert token is not None
    assert "win-1" in token


def test_identical_or_foreign_companions_are_not_conflicts():
    assert detect_companion_conflict(_companion(), _companion()) is None
    other = _companion(window_id="win-9")
    assert detect_companion_conflict(_companion(), other) is None


def test_non_objects_are_described_never_raised():
    assert detect_companion_conflict(None, None) is not None
    assert validate_admission_companion(None) != []
    assert validate_admission_companion("win-1") != []
