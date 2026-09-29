"""Bootstrap a MissionPlan and an initial Task Graph from an Engagement.

This is the minimal seam that gives the Task-first Orchestrator something to
schedule: an Engagement already carries "what is allowed" (scope/ROE), so a
run can open a Mission ("what are we trying to achieve") and seed one Task per
schedulable role ("what are we trying to do") without any operator questions.

Natural-language Intake / Engagement Planning (full Phase 2) layers on top of
this later; the legacy ``redteam run engagement.yaml`` path calls straight into
here, so it gains a Mission/Task Graph while its existing behaviour is
otherwise unchanged.

No secret ever reaches a Mission or Task: only the Engagement's own scope
metadata and role names are used.
"""

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
    store: MissionTaskStore,
    agents: AgentRegistry,
    engagement: Engagement,
    *,
    objective: str | None = None,
    now: datetime,
) -> MissionId:
    """Create the Mission and its initial (READY) Task Graph, return the id.

    The Mission's objective/priorities come from the Engagement Planner
    (``objective`` is an optional natural-language override; otherwise it is
    derived from the Engagement). One Task per schedulable role, priority in
    registry order (recon first), each bound to its role by ``assigned_agent``
    + ``required_capability``. Tasks start READY because they have no
    dependencies yet; dynamic replanning adds dependent Tasks as knowledge
    appears.
    """

    mission_id = MissionId(new_id("mission"))
    schedulable = [d for d in agents.definitions() if d.schedulable]
    provenance = Provenance(source="engagement:bootstrap")

    mission = plan_mission(engagement, mission_id, objective=objective, now=now)
    await store.create_mission(mission)
    explicit_focus = (
        set(mission.priorities) if objective or engagement.metadata.get("focus_areas") else set()
    )

    count = len(schedulable)
    for index, definition in enumerate(schedulable):
        capability = definition.capabilities[0] if definition.capabilities else ""
        matching_focus = {
            "recon": {"reconnaissance"},
            "web_api": {"web", "api", "authentication", "authorization"},
            "mobile": {"mobile"},
        }.get(definition.role, set())
        focus_rank = next(
            (
                position
                for position, focus in enumerate(mission.priorities)
                if focus in matching_focus
            ),
            len(mission.priorities),
        )
        # Recon seeds the surface other roles need. Among the remaining
        # workstreams, an explicit Mission priority beats registry order.
        priority = (
            (len(mission.priorities) + 1) * count + 1
            if definition.role == "recon"
            else (len(mission.priorities) - focus_rank) * count + (count - index)
        )
        task = MissionTask(
            task_id=TaskId(new_id("task")),
            mission_id=mission_id,
            title=definition.name,
            objective=f"Investigate the scope using the {definition.name} role",
            status=TaskStatus.READY,
            #: Higher runs first (see ``list_tasks`` ordering); registry order
            #: is the initial priority, so recon precedes web precedes mobile.
            priority=priority,
            required=(
                definition.role == "recon"
                or (definition.role == "web_api" and bool(
                    explicit_focus & {"web", "api", "authentication", "authorization"}
                ))
                or (definition.role == "mobile" and "mobile" in explicit_focus)
            ),
            assigned_agent=definition.agent_id,
            required_capability=capability or None,
            provenance=provenance,
            created_at=now,
            updated_at=now,
        )
        await store.create_task(task)

    return mission_id
