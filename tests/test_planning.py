import pytest

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import PlanPolicyViolation
from whs_redteam.models import PlanItem
from whs_redteam.models import TestCategory as Category
from whs_redteam.models import TestPlan as Plan
from whs_redteam.planning import PlanPolicy, RulesPlanner
from whs_redteam.policy.scope import ScopeGuard


def test_rules_planner_emits_only_bounded_categories() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    plan = RulesPlanner(config).plan(config.targets)
    assert len(plan.items) == 5
    assert {item.category for item in plan.items} == {
        Category.TRANSPORT_SECURITY,
        Category.SECURITY_HEADERS,
        Category.COOKIE_ATTRIBUTES,
        Category.CORS_CONFIGURATION,
        Category.INFORMATION_EXPOSURE,
    }
    PlanPolicy(ScopeGuard(config)).approve(plan)


def test_plan_policy_rejects_out_of_scope_target() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    plan = Plan(
        provider="rules",
        items=(
            PlanItem(
                target_url="https://outside.test",
                category=Category.SECURITY_HEADERS,
                reason="test",
            ),
        ),
    )
    with pytest.raises(PlanPolicyViolation, match="outside"):
        PlanPolicy(ScopeGuard(config)).approve(plan)


def test_plan_policy_rejects_unknown_provider_and_duplicates() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    item = PlanItem(
        target_url="https://example.test",
        category=Category.SECURITY_HEADERS,
        reason="test",
    )
    with pytest.raises(PlanPolicyViolation, match="rules"):
        PlanPolicy(ScopeGuard(config)).approve(
            Plan(provider="untrusted-ai", items=(item,))
        )
    with pytest.raises(PlanPolicyViolation, match="duplicate"):
        PlanPolicy(ScopeGuard(config)).approve(
            Plan(provider="rules", items=(item, item))
        )


def test_plan_policy_rejects_non_get_method() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    plan = Plan(
        provider="rules",
        items=(
            PlanItem(
                target_url="https://example.test",
                category=Category.SECURITY_HEADERS,
                reason="must remain read-only",
                method="POST",
            ),
        ),
    )
    with pytest.raises(PlanPolicyViolation, match="GET"):
        PlanPolicy(ScopeGuard(config)).approve(plan)
