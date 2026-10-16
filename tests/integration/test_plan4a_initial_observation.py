"""Candidate-free observation through real binding, registration, results and resume.

LLM and transport are explicit offline doubles; no provider or target is contacted.
"""

import pytest

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.enums import MissionStatus
from redteam.orchestration.hypothesis_planner import capture_snapshot
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TOOL, discover, gateway, inspect, turn
from tests.integration.test_central_parallel import limits
from tests.integration.test_hypothesis_planner import PlannerDouble, probe
from tests.integration.test_probe_workers import PROFILE, drive
from tests.integration.test_v3_atomization import SplitDouble, _atom_two_tasks, _plan_two_questions


@pytest.mark.parametrize("split", [True, False], ids=["split", "legacy"])
async def test_candidate_free_task_and_result_survive_storage_resume_and_report(tmp_path, split):
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    profile = PROFILE if split else PROFILE.with_name("native-probes-legacy.yaml")
    configured = configured.with_overrides(specialist_profile_path=str(profile))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    llm = (SplitDouble([_plan_two_questions()], [_atom_two_tasks()]) if split else
           PlannerDouble([{"updates": [], "tasks": [probe()],
                           "termination": "continue", "reason": "Observe before hypothesizing"}]))
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    root = tmp_path / "run"
    async with open_central_run(config, directory=root, settings=configured, **options) as session:
        planner = SpecialistPlanner(session.deps)
        identities = await planner.advance(session.runtime)
        assert len(identities) == (2 if split else 1)
        assert await planner.advance(session.runtime) == []
        state = session.deps.central_state
        snapshot = await state.transaction(capture_snapshot, write=False)
        assert snapshot.candidates == {}
        if split:
            assert all(q["hypothesis_revisions"] == {} for q in llm.atom_inputs[0]["questions"])
        for index, identity in enumerate(identities):
            task = await state.get_task(identity)
            assert task.hypothesis_revisions == {} and task.related_hypothesis_ids == []
            assert all(c.hypothesis_id is None for c in task.completion_criteria)
            llm.native_responses_by_task[identity] = [
                discover(TOOL), turn(inspect("original", account=str(index))),
            ]
        outcomes = await drive(session)
        assert all(outcome.kind == "completed" for outcome in outcomes)
        saved = {identity: await state.get_task(identity) for identity in identities}
        for task in saved.values():
            assert task.result is not None and task.result.evidence_ids
            assert task.result.hypothesis_ids == []
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        request_count = len(llm.requests)

    async with open_central_run(
        config, directory=root, settings=configured, resume=True,
        run_id=run_id, engagement_id=engagement_id, **options,
    ) as session:
        assert {identity: await session.deps.central_state.get_task(identity)
                for identity in identities} == saved
        graph = AttackGraphService(session.deps.graph_store)
        assert await graph.list_hypotheses() == [] and await graph.list_findings() == []
        snapshot = await session.deps.central_state.transaction(capture_snapshot, write=False)
        assert snapshot.candidates == {}
        assert snapshot.mission.status is MissionStatus.ACTIVE
        _, _, report = await inspect_central_run(session.deps.central_state.path, str(run_id))
        assert report.candidates == [] and report.findings == []
        assert len(report.investigations) == len(saved)
        for investigation in report.investigations:
            assert investigation["hypothesis_revisions"] == {}
            assert investigation["result"]["hypothesis_ids"] == []
        assert len(llm.requests) == request_count
        assert len(tools.inner.invocations) == len(saved)
