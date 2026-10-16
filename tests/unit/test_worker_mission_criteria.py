"""The canonical Mission criteria remain visible in task-local worker context."""

import json
from types import SimpleNamespace

import pytest

from redteam.core.enums import MissionStatus
from redteam.orchestration.worker_conversation import update_session


class _Session:
    def __init__(self):
        self.notifications = (("mission", "previous-value"),)
        self.messages = []

    def conversation(self):
        return []

    def tools(self):
        return []

    def append(self, *messages):
        self.messages.extend(messages)
        return self

    def model_copy(self, *, update):
        self.notifications = update["notifications"]
        return self


@pytest.mark.asyncio
async def test_worker_update_projects_canonical_mission_success_criteria():
    criteria = ["Compare the registered account outcomes"]
    mission = SimpleNamespace(
        objective="Assess the registered response",
        status=MissionStatus.ACTIVE,
        operator_preferences={},
        success_criteria=criteria,
    )

    class CentralState:
        async def get_mission(self, _mission_id):
            return mission

        async def list_for_mission(self, _mission_id):
            return []

    deps = SimpleNamespace(
        local_tools=SimpleNamespace(descriptors=lambda: []),
        tool_catalog=SimpleNamespace(get=lambda _identity: None),
        central_state=CentralState(),
        engagement=SimpleNamespace(
            scope=SimpleNamespace(model_dump=lambda mode=None: {"allowed_hosts": ["host"]}),
            rules_of_engagement=(),
        ),
        control_plane=None,
        graph_store=object(),
        shell_programs=None,
    )
    runtime = SimpleNamespace(
        mission_id="mission-1",
        run_id="run-1",
        engagement_id="engagement-1",
        active_task_id="task-1",
        probe_hypotheses={},
    )
    session = _Session()

    updated = await update_session(
        session,
        deps=deps,
        runtime=runtime,
        context={"active_task": {}, "tools_by_role": {}},
    )

    assert updated.messages
    notice = json.loads(updated.messages[0].message()["content"])
    assert notice["worker_updates"]["mission"]["success_criteria"] == criteria
