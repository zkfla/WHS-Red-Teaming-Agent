"""Central execution after retiring the legacy engine; temporary doubles only."""

import asyncio
from types import SimpleNamespace

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.cli import _build_parser
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse
from redteam.core.config import OrchestrationConfig
from redteam.core.enums import MissionStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.graph import build_graph
from redteam.orchestration.runner import AgentLoopRunner
from tests.integration.test_central_completion import (
    OriginalProtocolDouble,
    finish_inconclusive,
    review,
    selection,
)
from tests.integration.test_central_development import gateway, proposal, register
from tests.integration.test_central_parallel import drain, limits


@pytest.mark.parametrize("command", [
    ["run", "engagement.yaml"],
    ["resume", "original", "engagement.yaml"],
    ["status", "original"],
    ["findings", "list", "original"],
    ["report", "generate", "original"],
    ["approvals", "list"],
])
def test_legacy_option_is_rejected_before_any_execution(command, capsys):
    with pytest.raises(SystemExit) as error:
        _build_parser().parse_args([*command, "--runtime", "legacy"])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_legacy_environment_selection_is_rejected():
    with pytest.raises(ValueError):
        OrchestrationConfig.from_environment({"REDTEAM_RUNTIME_BACKEND": "legacy"})


@pytest.mark.parametrize("constructor", [build_graph, AgentLoopRunner])
def test_missing_central_owner_cannot_start_an_alternate_engine(constructor):
    with pytest.raises(ValueError, match="runtime_requires_central_state"):
        constructor(SimpleNamespace(central_state=None))


async def test_free_worker_slots_admit_more_than_eight_investigations():
    engagement, settings = limits(requests=100)
    llm = FakeLLMClient()
    async with build_central_development(
        engagement, llm=llm, tools=gateway(), settings=settings,
    ) as session:
        for index in range(9):
            identity, = await register(session, f"retirement-{index}")
            llm.native_responses_by_task[identity] = [
                NativeCompletion(text="More investigation is needed", total_tokens=1),
            ]
        outcomes = await asyncio.wait_for(drain(session), 10)
        assert len(outcomes) == 9
        assert all(task.occupant_owner is not None
                   for task in await session.deps.central_state.list_tasks())
        assert await session.remaining_budget() == 12


async def test_planner_can_judge_new_results_after_four_strategies():
    candidate = proposal()
    strategy = {"questions": [q.model_dump(mode="json") for q in candidate.questions],
                "reason": "Another useful investigation"}
    atomization = {"tasks": [t.model_dump(mode="json") for t in candidate.tasks]}
    llm = FakeLLMClient(
        structured_responses=[
            *(item for _ in range(5) for item in (strategy, atomization)),
            {"questions": [], "reason": "Wait for further evidence", "termination": "held"},
        ],
        native_responses=[NativeCompletion(text="More investigation needed", total_tokens=1)
                          for _ in range(8)],
    )
    engagement, settings = limits(requests=100)
    async with build_central_development(
        engagement, llm=llm, tools=gateway(), settings=settings,
    ) as session:
        await asyncio.wait_for(session.run(settle=False), 10)
        assert len(await session.deps.central_state.list_tasks()) == 5
        usage = await session.deps.central_state.request_ledger().snapshot(
            str(session.runtime.run_id),
        )
        assert usage["spent"] < 100


async def test_missing_creation_declaration_cannot_become_goal_success(tmp_path):
    tools = gateway()
    tools.responses[0] = DevelopmentResponse(
        output={"resource_id": "synthetic-created-object", "created": True}, metadata={},
    )
    async with selection(tmp_path, tools=OriginalProtocolDouble(tools)) as (session, llm):
        refs = await finish_inconclusive(session, llm)
        assert (await AttackGraphService(session.deps.graph_store).cleanup_report()).items == []
        assert "resource_effects_unmapped_cleanup_unverified" in await review(session, llm, refs)
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE

