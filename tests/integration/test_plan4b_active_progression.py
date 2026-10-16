"""Offline observation -> grounded candidate -> bounded verification flow.

The model and target transport are explicit doubles. This proves contract propagation,
catalog visibility, admission and immutable originals; it does not contact a live target.
"""

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.enums import MissionStatus
from redteam.orchestration.agents.tool_access import eligible_role_tool_ids
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_probe_workers import drive
from tests.integration.test_probe_workers import selection as probe_selection
from tests.integration.test_v3_atomization import SplitDouble


def _criterion(description: str) -> str:
    return criterion_id(CompletionCriterion(kind="observation", description=description))


async def test_observation_becomes_grounded_candidate_and_distinguishing_probe(tmp_path):
    observe_description = "Preserve one bounded response receipt"
    verify_description = (
        "Preserve a receipt that distinguishes accepted, rejected and failed handling"
    )
    first_plan = {
        "updates": [],
        "questions": [{
            "question_id": "observe",
            "objective": "Observe the registered input surface and its response boundary",
            "target_node_ids": [TARGET],
            "candidate_refs": [],
            "completion_criteria": [{
                "kind": "observation", "description": observe_description,
            }],
        }],
        "reason": "Collect an original before proposing a security premise",
        "termination": "continue",
    }
    first_atom = {
        "tasks": [{
            "task_key": "observe",
            "question_id": "observe",
            "objective": "Observe the registered input surface and its response boundary",
            "title": "Input surface observation",
            "role_id": "probe-worker",
            "target_node_ids": [TARGET],
            "completion_criterion_ids": [_criterion(observe_description)],
            "probe_limits": {"max_actions": 1, "max_model_requests": 4},
        }],
    }

    captured: dict[str, str] = {}

    def grounded_plan(payload):
        source = next(
            task for task in payload["investigations"]
            if task["result"] is not None and task["hypothesis_revisions"] == {}
        )
        original = next(
            record for record in payload["original_observations"]["records"]
            if record["evidence_id"] in source["result"]["evidence_ids"]
        )
        captured["source_task_id"] = source["task_id"]
        captured["evidence_id"] = original["evidence_id"]
        return {
            "updates": [{
                "expected_revision": None,
                "hypothesis": {
                    "contract_version": 2,
                    "hypothesis_id": "observed-premise",
                    "description": (
                        "The observed input surface may handle a bounded variant across "
                        "a security boundary without the expected distinction"
                    ),
                    "target_node_ids": [TARGET],
                    "reason": (
                        "Observed fact: the retained response exposes an input surface. "
                        "Untested premise: a bounded variant may cross the expected boundary."
                    ),
                    "status": "open",
                    "supporting_evidence_ids": [original["evidence_id"]],
                },
            }],
            "questions": [{
                "question_id": "verify",
                "objective": (
                    "Compare one bounded variant with its control and retain both outcomes"
                ),
                "target_node_ids": [TARGET],
                "candidate_refs": ["observed-premise"],
                "evidence_ids": [original["evidence_id"]],
                "source_task_id": source["task_id"],
                "completion_criteria": [{
                    "kind": "observation", "description": verify_description,
                }],
            }],
            "reviewed_task_ids": [source["task_id"]],
            "reason": "Use the original to test one distinguishing security premise",
            "termination": "continue",
        }

    def verification_atom(payload):
        question, = payload["questions"]
        candidate_id, = question["hypothesis_revisions"]
        assert question["source_task_id"] == captured["source_task_id"]
        assert question["evidence_ids"] == [captured["evidence_id"]]
        assert payload["candidate_revisions"] == {candidate_id: 1}
        return {
            "tasks": [{
                "task_key": "verify",
                "question_id": "verify",
                "objective": question["objective"],
                "title": "Bounded boundary comparison",
                "role_id": "probe-worker",
                "target_node_ids": [TARGET],
                "evidence_ids": [captured["evidence_id"]],
                "completion_criterion_ids": [_criterion(verify_description)],
                "probe_limits": {"max_actions": 1, "max_model_requests": 4},
            }],
        }

    llm = SplitDouble([first_plan, grounded_plan], [first_atom, verification_atom])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        first_id, = await planner.advance(session.runtime)
        first = await session.deps.central_state.get_task(first_id)
        assert first.hypothesis_revisions == {}
        banned = ("폼 제출 금지", "자격증명 사용 금지", "인증 시도 금지", "모든 상태 변경 금지")
        assert not any(item in first.objective for item in banned)

        llm.native_responses_by_task[first_id] = [
            discover(TOOL), turn(inspect("observed-input-surface")),
        ]
        first_outcome, = await drive(session)
        assert first_outcome.kind == "completed"

        second_id, = await planner.advance(session.runtime)
        candidate, = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        second = await session.deps.central_state.get_task(second_id)
        assert candidate.supporting_evidence_ids == [captured["evidence_id"]]
        assert "Observed fact:" in candidate.reason
        assert "Untested premise:" in candidate.reason
        assert second.source_task_id == first_id
        assert second.evidence_ids == [captured["evidence_id"]]
        assert second.hypothesis_revisions == {candidate.hypothesis_id: 1}
        assert not any(item in second.objective for item in banned)
        assert TOOL in await eligible_role_tool_ids(session.deps, "probe-worker")

        llm.native_responses_by_task[second_id] = [
            discover(TOOL), turn(inspect("bounded-distinguishing-check")),
        ]
        second_outcome, = await drive(session)
        assert second_outcome.kind == "completed"
        assert len(tools.inner.invocations) == 2
        assert len(await session.deps.evidence_store.list_for_run(session.runtime.run_id)) >= 2
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
