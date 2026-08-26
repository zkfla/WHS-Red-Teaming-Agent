"""Engagement 스키마 + Policy Compiler 단위 테스트.

핵심 불변식(engagement §9): Effective Policy = Engagement ∩ Global Safety
Ceiling — Engagement 가 전역 상한보다 느슨해질 수 없다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from finredteam import config
from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import (
    Engagement,
    EngagementActions,
    EngagementRequests,
    EngagementTarget,
)


def _engagement(**overrides) -> Engagement:
    base = dict(
        engagement_id="test-eng",
        target=EngagementTarget(name="test-target", base_url="http://localhost:5000"),
    )
    base.update(overrides)
    return Engagement(**base)


def test_numeric_ceilings_clamp_to_global_maximum():
    engagement = _engagement(
        requests=EngagementRequests(
            max_requests_per_second=9999.0, max_concurrency=9999, max_amount=Decimal("999999")
        )
    )
    policy = compile_policy(engagement)
    assert policy.max_requests_per_second == config.MAX_REQUESTS_PER_SECOND
    assert policy.max_concurrency == config.MAX_CONCURRENT
    assert policy.max_amount == config.MAX_AMOUNT


def test_numeric_values_below_ceiling_are_kept():
    engagement = _engagement(
        requests=EngagementRequests(max_requests_per_second=1.0, max_concurrency=1, max_amount=Decimal("50"))
    )
    policy = compile_policy(engagement)
    assert policy.max_requests_per_second == 1.0
    assert policy.max_concurrency == 1
    assert policy.max_amount == Decimal("50")


def test_max_amount_none_falls_back_to_global_ceiling_only():
    engagement = _engagement(requests=EngagementRequests(max_amount=None))
    policy = compile_policy(engagement)
    assert policy.max_amount == config.MAX_AMOUNT


def test_dangerous_booleans_are_always_false_regardless_of_engagement():
    engagement = _engagement(
        actions=EngagementActions(allow_dos=True, allow_bruteforce=True, allow_race_testing=True)
    )
    policy = compile_policy(engagement)
    assert policy.allow_dos is False
    assert policy.allow_bruteforce is False
    assert policy.allow_race_testing is False


def test_permissive_booleans_follow_engagement_when_true():
    engagement = _engagement(actions=EngagementActions(allow_state_change=True, allow_delete=True))
    policy = compile_policy(engagement)
    assert policy.allow_state_change is True
    assert policy.allow_delete is True


def test_permissive_booleans_default_false_when_engagement_denies():
    engagement = _engagement()  # 기본값 전부 False
    policy = compile_policy(engagement)
    assert policy.allow_state_change is False
    assert policy.allow_account_creation is False


def test_policy_hash_is_deterministic_for_identical_engagement():
    e1 = _engagement()
    e2 = _engagement()
    assert compile_policy(e1).policy_hash == compile_policy(e2).policy_hash


def test_policy_hash_changes_when_scope_changes():
    from finredteam.engagement.schema import EngagementScope

    e1 = _engagement(scope=EngagementScope(allowed_hosts=["a.com"]))
    e2 = _engagement(scope=EngagementScope(allowed_hosts=["b.com"]))
    assert compile_policy(e1).policy_hash != compile_policy(e2).policy_hash


def test_decimal_amount_round_trips_through_yaml_without_float_drift(tmp_path):
    """§4 규칙 2 — Decimal-only. yaml 저장/로드 후에도 부동소수점 오차 없이
    정확한 값이 유지되는지 확인한다."""
    from finredteam.engagement import store as store_module
    from finredteam.engagement.loader import load_engagement

    engagement = _engagement(requests=EngagementRequests(max_amount=Decimal("999.99")))
    dir_path = store_module.save_engagement(engagement, root=tmp_path)
    loaded = load_engagement(dir_path)
    assert loaded.requests.max_amount == Decimal("999.99")


@pytest.mark.parametrize("method,expected", [("GET", True), ("HEAD", True), ("OPTIONS", True)])
def test_safe_methods_always_allowed(method, expected):
    engagement = _engagement()  # allow_state_change=False 기본값
    policy = compile_policy(engagement)
    assert policy.is_method_allowed(method) is expected


def test_state_change_method_blocked_when_not_allowed():
    engagement = _engagement()
    policy = compile_policy(engagement)
    assert policy.is_method_allowed("POST") is False


def test_state_change_method_allowed_when_engagement_permits():
    engagement = _engagement(actions=EngagementActions(allow_state_change=True))
    policy = compile_policy(engagement)
    assert policy.is_method_allowed("POST") is True
    assert policy.is_method_allowed("DELETE") is False  # allow_delete 는 별도


def test_delete_requires_explicit_allow_delete():
    engagement = _engagement(actions=EngagementActions(allow_state_change=True, allow_delete=True))
    policy = compile_policy(engagement)
    assert policy.is_method_allowed("DELETE") is True


# ── Stop Condition 감지(§ agent/react.py) 를 위한 필드 컴파일 ──────────


def test_stop_condition_defaults():
    from finredteam.engagement.schema import EngagementStopConditions

    defaults = EngagementStopConditions()
    # 안전/Scope 보호 직결 — 기본 활성화 (사용자 확정 설계 결정).
    assert defaults.unexpected_sensitive_data is True
    # 오탐 위험 — 신뢰도 검증 전까지는 opt-in.
    assert defaults.latency_degradation is False
    # Hard Stop 미지원 — 항상 opt-in(사실상 미사용).
    assert defaults.external_side_effect is False


def test_latency_and_sensitive_data_stop_fields_pass_through():
    from finredteam.engagement.schema import EngagementStopConditions

    engagement = _engagement(
        stop_conditions=EngagementStopConditions(
            latency_degradation=True,
            latency_degradation_multiplier=5.0,
            latency_degradation_consecutive_threshold=3,
            unexpected_sensitive_data=True,
            unexpected_sensitive_data_threshold=2,
        )
    )
    policy = compile_policy(engagement)
    assert policy.stop_latency_degradation is True
    assert policy.stop_latency_degradation_multiplier == 5.0
    assert policy.stop_latency_degradation_consecutive_threshold == 3
    assert policy.stop_unexpected_sensitive_data is True
    assert policy.stop_unexpected_sensitive_data_threshold == 2


def test_stop_condition_change_affects_policy_hash():
    from finredteam.engagement.schema import EngagementStopConditions

    e1 = _engagement(stop_conditions=EngagementStopConditions(latency_degradation=False))
    e2 = _engagement(stop_conditions=EngagementStopConditions(latency_degradation=True))
    assert compile_policy(e1).policy_hash != compile_policy(e2).policy_hash
