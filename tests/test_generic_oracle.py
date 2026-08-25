"""oracles/generic.py 단위 테스트 — Generic Layer 는 ANOMALY 비율이 높은 게 정상이다(§15)."""
from __future__ import annotations

from finredteam.models.core import Verdict
from finredteam.oracles.generic import (
    check_bola_signature,
    check_jwt_none_alg,
    check_reflected_xss,
    check_sqli_signature,
)


def test_sqli_violation_needs_error_and_differential():
    outcome = check_sqli_signature("psycopg2.errors.SyntaxError: ...", boolean_diff=True)
    assert outcome.verdict == Verdict.VIOLATION


def test_sqli_error_only_is_anomaly_not_violation():
    outcome = check_sqli_signature("You have an error in your SQL syntax", boolean_diff=False, time_delay=False)
    assert outcome.verdict == Verdict.ANOMALY


def test_sqli_no_signal_passes():
    assert check_sqli_signature("hello world", boolean_diff=False).verdict == Verdict.PASS


def test_xss_violation_needs_unescaped_executable_context():
    outcome = check_reflected_xss(reflected=True, escaped=False, executable_context=True)
    assert outcome.verdict == Verdict.VIOLATION


def test_xss_string_only_reflection_is_anomaly():
    outcome = check_reflected_xss(reflected=True, escaped=True, executable_context=False)
    assert outcome.verdict == Verdict.ANOMALY


def test_bola_violation_needs_entitlement_known_false():
    outcome = check_bola_signature(cross_principal_object_returned=True, entitlement_known=False)
    assert outcome.verdict == Verdict.VIOLATION


def test_bola_200_only_is_anomaly():
    outcome = check_bola_signature(cross_principal_object_returned=True, entitlement_known=None)
    assert outcome.verdict == Verdict.ANOMALY


def test_jwt_none_alg_accepted_is_violation():
    assert check_jwt_none_alg(accepted_unsigned=True).verdict == Verdict.VIOLATION


def test_jwt_alg_mutable_but_rejected_is_anomaly():
    outcome = check_jwt_none_alg(accepted_unsigned=False, alg_header_mutable=True)
    assert outcome.verdict == Verdict.ANOMALY
