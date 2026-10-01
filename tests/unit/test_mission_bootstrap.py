"""Phase 3: an Engagement bootstraps a Mission + an initial Task Graph."""

from datetime import datetime, timezone

from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.mission_bootstrap import bootstrap_mission
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_boot"),
        name="VulnBank",
        scope=EngagementScope(allowed_hosts=["mock-bank"]),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


async def test_bootstrap_creates_active_mission_and_ready_tasks() -> None:
    store = InMemoryTaskGraphStore()
    agents = default_registry()
    now = datetime.now(timezone.utc)

    mission_id = await bootstrap_mission(store, agents, _engagement(), now=now)

    mission = await store.get_mission(mission_id)
    assert mission is not None
    assert mission.status is MissionStatus.ACTIVE
    assert mission.objective  # non-blank

    tasks = await store.list_tasks(mission_id=mission_id)
    assert len(tasks) == 1
    assert all(task.status is TaskStatus.READY for task in tasks)
    assert tasks[0].objective == mission.objective
    # The operator goal is seeded before the model chooses investigation roles.
    assert tasks[0].assigned_agent is None
    assert tasks[0].required_capability is None


async def test_bootstrap_carries_no_secret_material() -> None:
    store = InMemoryTaskGraphStore()
    mission_id = await bootstrap_mission(
        store, default_registry(), _engagement(), now=datetime.now(timezone.utc)
    )
    mission = await store.get_mission(mission_id)
    assert mission is not None
    dumped = mission.model_dump_json().lower()
    for secret in ("password", "token", "cookie", "bearer"):
        assert secret not in dumped


async def test_mobile_goal_is_preserved_for_llm_role_selection() -> None:
    store = InMemoryTaskGraphStore()
    mission_id = await bootstrap_mission(
        store, default_registry(), _engagement(),
        objective="모바일 앱 점검을 우선해줘",
        now=datetime.now(timezone.utc),
    )

    tasks = await store.list_tasks(mission_id=mission_id)
    mission = await store.get_mission(mission_id)
    assert mission is not None
    assert mission.objective == "모바일 앱 점검을 우선해줘"
    assert len(tasks) == 1
    assert tasks[0].objective == mission.objective
    assert tasks[0].assigned_agent is None
