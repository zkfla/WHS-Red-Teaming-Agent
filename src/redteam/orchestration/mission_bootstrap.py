"""Store the operator's goal without choosing the first investigation."""
from __future__ import annotations

from datetime import datetime

from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.enums import TaskStatus
from redteam.core.ids import MissionId, TaskId, new_id
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.engagement_planner import plan_mission


async def bootstrap_mission(
    store: MissionTaskStore, agents: AgentRegistry, engagement: Engagement,
    *, objective: str | None = None, now: datetime,
) -> MissionId:
    mission_id = MissionId(new_id("mission"))
    mission = plan_mission(engagement, mission_id, objective=objective, now=now)
    await store.create_mission(mission)
    await store.create_task(MissionTask(
        task_id=TaskId(new_id("task")), mission_id=mission_id,
        title="Investigate the operator goal", objective=mission.objective,
        status=TaskStatus.READY, required=True,
        provenance=Provenance(source="engagement:goal"), created_at=now, updated_at=now,
    ))
    return mission_id
