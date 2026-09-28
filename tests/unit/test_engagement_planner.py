"""Phase 2: Engagement Planner turns intent into a MissionPlan (LLM optional)."""

from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.ids import EngagementId, MissionId
from redteam.orchestration.engagement_planner import (
    extract_focus_areas,
    parse_objective,
    plan_mission,
)


def _engagement(**metadata: Any) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_plan"),
        name="VulnBank",
        scope=EngagementScope(allowed_hosts=["mock-bank"]),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata=dict(metadata),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_focus_extraction_handles_korean_and_english() -> None:
    assert extract_focus_areas("인증/인가 중심으로 봐줘") == ["authentication", "authorization"]
    assert extract_focus_areas("check the API auth") == ["authentication", "api"]


def test_plan_mission_derives_objective_and_priorities_from_intent() -> None:
    mission = plan_mission(
        _engagement(),
        MissionId("m1"),
        objective="VulnBank 웹 레드티밍, 인증/인가 중심",
        now=_now(),
    )
    assert "VulnBank" in mission.objective
    assert mission.priorities == ["authentication", "authorization", "web"]
    assert mission.initial_workstreams == mission.priorities
    assert all("verified evidence" in criterion for criterion in mission.success_criteria)


def test_plan_mission_falls_back_to_engagement_when_no_objective() -> None:
    mission = plan_mission(
        _engagement(focus_areas=["mobile"]), MissionId("m2"), now=_now()
    )
    assert "VulnBank" in mission.objective  # derived from engagement name
    assert mission.priorities == ["mobile"]  # recorded focus honoured


async def test_parse_objective_is_deterministic_without_an_llm() -> None:
    objective, focus = await parse_objective("인가 우회 봐줘")
    assert objective == "인가 우회 봐줘"
    assert focus == ["authorization"]


async def test_parse_objective_ignores_a_secret_looking_llm_rewrite() -> None:
    class _LeakyLLM:
        async def complete_structured(self, **_: Any) -> dict[str, Any]:
            return {"objective": "use bearer token abc123 to log in"}

    objective, _focus = await parse_objective("인증 점검", llm=_LeakyLLM())
    # The secret-looking rewrite is rejected; the operator's words stand.
    assert objective == "인증 점검"


async def test_parse_objective_uses_a_clean_llm_rewrite() -> None:
    class _CleanLLM:
        async def complete_structured(self, **_: Any) -> dict[str, Any]:
            return {"objective": "Assess authentication on the web tier"}

    objective, focus = await parse_objective("인증 좀", llm=_CleanLLM())
    assert objective == "Assess authentication on the web tier"
    assert "authentication" in focus
