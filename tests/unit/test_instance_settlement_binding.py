"""Instance limits cannot close a shared mission execution binding."""

import pytest

from redteam.app.bootstrap import settle_engagement
from redteam.core.enums import MissionStatus
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.state import TerminationReason
from tests.fakes import build_test_application
from tests.unit.test_orchestration_final_boundaries import setup


@pytest.mark.parametrize("instance_id,closed", [("one", False), (None, True)])
async def test_local_limit_preserves_shared_binding_legacy_behavior_retained(
    monkeypatch, instance_id, closed,
):
    template_deps, _coordinator, runtime, _publisher = await setup()
    mission = await template_deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    deps = build_test_application()
    await deps.task_graph_store.create_mission(mission.model_copy(update={
        "status": MissionStatus.ACTIVE,
    }))
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    await coordinator.bind(str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id))
    runtime = runtime.model_copy(update={
        "instance_id": instance_id,
        "termination_reason": TerminationReason.MAX_STEPS_REACHED,
        "mission_status": MissionStatus.ACTIVE,
    })

    async def project(*args, **kwargs):
        return None

    # This test owns the composition close boundary. The canonical mission
    # finalizer's multi-instance decisions are exercised in separate tests.
    monkeypatch.setattr("redteam.app.bootstrap.finalize_mission", project)
    monkeypatch.setattr("redteam.app.bootstrap._publish_terminal_artifacts", project)
    await settle_engagement(runtime, deps)

    async def inspect(change):
        return change.binding(str(runtime.run_id), str(runtime.mission_id))["closed"]

    assert await coordinator.change(inspect) is closed


@pytest.mark.parametrize("before,after,closed", [
    (MissionStatus.COMPLETED, MissionStatus.ACTIVE, False),
    (MissionStatus.ACTIVE, MissionStatus.CANCELLED, True),
])
async def test_close_reads_canonical_mission_inside_its_transaction(
    monkeypatch, before, after, closed,
):
    template_deps, _coordinator, runtime, _publisher = await setup()
    mission = await template_deps.task_graph_store.get_mission(runtime.mission_id)
    assert mission is not None
    deps = build_test_application()
    await deps.task_graph_store.create_mission(mission.model_copy(update={
        "status": MissionStatus.ACTIVE,
    }))
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    await coordinator.bind(str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id))
    async def set_initial_status(changes):
        current = await changes.tasks.get_mission(runtime.mission_id)
        await changes.tasks.update_mission(current.model_copy(update={
            "status": before, "revision": current.revision + 1,
        }))

    await coordinator.change(set_initial_status)
    runtime = runtime.model_copy(update={
        "instance_id": "one", "termination_reason": TerminationReason.MAX_STEPS_REACHED,
    })

    async def project(*args, **kwargs):
        return None

    async def change_between_projection_and_close(*args, **kwargs):
        async def change(changes):
            current = await changes.tasks.get_mission(runtime.mission_id)
            await changes.tasks.update_mission(current.model_copy(update={
                "status": after, "revision": current.revision + 1,
            }))
        await coordinator.change(change)
        return 0

    monkeypatch.setattr("redteam.app.bootstrap.finalize_mission", project)
    monkeypatch.setattr("redteam.app.bootstrap._publish_terminal_artifacts", project)
    monkeypatch.setattr(
        "redteam.app.bootstrap.deliver_pending", change_between_projection_and_close,
    )
    await settle_engagement(runtime, deps)

    async def inspect(changes):
        return changes.binding(str(runtime.run_id), str(runtime.mission_id))["closed"]

    assert await coordinator.change(inspect) is closed
