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
    schedulable = [d for d in agents.definitions() if d.schedulable]
    assert len(tasks) == len(schedulable)
    assert all(task.status is TaskStatus.READY for task in tasks)

    # Every schedulable role is represented and bound to its agent/capability.
    by_agent = {task.assigned_agent: task for task in tasks}
    for definition in schedulable:
        task = by_agent[definition.agent_id]
        assert task.required_capability == (definition.capabilities[0] or None)

    # Registry order sets priority: recon runs before web before mobile.
    ordered = [task.assigned_agent for task in tasks]  # list_tasks sorts by -priority
    assert ordered[0] == schedulable[0].agent_id


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


async def test_explicit_mobile_focus_changes_post_recon_task_priority() -> None:
    store = InMemoryTaskGraphStore()
    mission_id = await bootstrap_mission(
        store, default_registry(), _engagement(),
        objective="모바일 앱 점검을 우선해줘",
        now=datetime.now(timezone.utc),
    )

    tasks = await store.list_tasks(mission_id=mission_id)
    assert [str(task.assigned_agent) for task in tasks] == [
        "recon-agent", "mobile-agent", "web-api-agent",
    ]
