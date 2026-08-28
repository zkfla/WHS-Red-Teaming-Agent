"""Rules-only planner with an interface for future structured providers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import PlanPolicyViolation
from whs_redteam.models import PlanItem, TestCategory, TestPlan
from whs_redteam.policy.scope import ScopeGuard


class Planner(Protocol):
    """Future local or API planners must return this same bounded schema."""

    def plan(self, targets: Iterable[str]) -> TestPlan: ...


class RulesPlanner:
    def __init__(self, config: EngagementConfig) -> None:
        self.config = config

    def plan(self, targets: Iterable[str]) -> TestPlan:
        categories = [
            TestCategory.TRANSPORT_SECURITY,
            TestCategory.SECURITY_HEADERS,
            TestCategory.COOKIE_ATTRIBUTES,
            TestCategory.CORS_CONFIGURATION,
            TestCategory.INFORMATION_EXPOSURE,
        ]
        if self.config.actors:
            categories.append(TestCategory.ACTOR_RESPONSE_COMPARISON)
        items = tuple(
            PlanItem(
                target_url=target,
                category=category,
                reason="Selected by the deterministic Day 4 rules planner.",
            )
            for target in dict.fromkeys(targets)
            for category in categories
        )
        return TestPlan(provider="rules", items=items)


class PlanPolicy:
    def __init__(self, scope_guard: ScopeGuard) -> None:
        self.scope_guard = scope_guard

    def approve(self, plan: TestPlan) -> TestPlan:
        if plan.provider != "rules":
            raise PlanPolicyViolation("only the rules planner is enabled in this prototype")
        if not plan.items:
            raise PlanPolicyViolation("test plan is empty")
        seen: set[tuple[str, TestCategory]] = set()
        for item in plan.items:
            if item.method != "GET":
                raise PlanPolicyViolation("planned tests may only use GET")
            decision = self.scope_guard.evaluate(item.target_url, item.method)
            if not decision.allowed:
                raise PlanPolicyViolation(
                    f"planned target is outside the approved scope: {decision.reason}"
                )
            key = (item.target_url, item.category)
            if key in seen:
                raise PlanPolicyViolation("test plan contains duplicate items")
            seen.add(key)
        return plan

    @staticmethod
    def categories_by_target(plan: TestPlan) -> dict[str, set[TestCategory]]:
        result: dict[str, set[TestCategory]] = {}
        for item in plan.items:
            result.setdefault(item.target_url, set()).add(item.category)
        return result
