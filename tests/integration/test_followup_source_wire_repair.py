"""Stage-1 follow-up source repair through the real split planner loop."""

import json

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import TaskStatus
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import selection
from tests.integration.test_v3_atomization import SplitDouble, _condition_id
from tests.unit.test_central_state import task

SOURCE_TASK_ID = "captured-source-must-not-echo"
SOURCE_RESULT_ID = "captured-result"
FOLLOWUP_CODE = "plan_question_followup_invalid"


def _plan(*, explicit_null):
    question = {
        "question_id": "followup-q",
        "objective": "Inspect the unresolved response premise",
        "target_node_ids": [TARGET],
        "candidate_refs": [],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
        "source_task_id": SOURCE_TASK_ID,
    }
    if explicit_null:
        question["source_result_id"] = None
    return {
        "updates": [],
        "questions": [question],
        "reason": "Use the captured original to inspect its unresolved premise",
        "termination": "continue",
    }


class RepairSplitDouble(SplitDouble):
    def __init__(self):
        super().__init__([_plan(explicit_null=True), _plan(explicit_null=False)], [])
        self.plan_feedback = []
        self.observed_before_plan = []
        self.observer = None

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "questions" in response_schema.get("properties", {}):
            prompt = messages[-1]["content"]
            _, _, feedback = prompt.partition("\n\nPrevious attempt failed")
            self.plan_feedback.append(feedback)
            if self.observer is not None:
                self.observed_before_plan.append(await self.observer())
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


async def test_explicit_null_followup_source_repairs_then_atomizes_once(tmp_path):
    llm = RepairSplitDouble()

    def atomize(payload):
        question, = payload["questions"]
        assert question["source_task_id"] == SOURCE_TASK_ID
        assert question["source_result_id"] == SOURCE_RESULT_ID
        assert question["completion_criteria"][0]["description"] == "Inspect response"
        return {"tasks": [{
            "task_key": "followup-task",
            "question_id": "followup-q",
            "objective": "Inspect the unresolved response premise",
            "title": "Follow-up observation",
            "role_id": "probe-worker",
            "target_node_ids": [TARGET],
            "completion_criterion_ids": [_condition_id("Inspect response")],
            "probe_limits": {"max_actions": 1, "max_model_requests": 4},
        }]}

    llm.atoms = [atomize]
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        state = session.deps.central_state
        source = task(SOURCE_TASK_ID, mission_id=str(session.runtime.mission_id)).model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "required": False,
                "objective": "Observe the original response",
                "result": InvestigationResult.model_validate({
                    "contract_version": 2,
                    "result_id": SOURCE_RESULT_ID,
                    "outcome": "inconclusive",
                    "summary": "The original response left one premise unresolved",
                    "observations": ["The response was observed"],
                    "limitations": ["Authorization remains untested"],
                }),
            },
        )
        await state.transaction(lambda tx: tx.db.execute(
            "INSERT INTO state_tasks VALUES(?,?,?)",
            (tx.run_id, str(source.task_id), source.model_dump_json()),
        ))

        async def committed_counts():
            return len(await state.list_tasks()), len(
                await AttackGraphService(session.deps.graph_store).list_hypotheses()
            )

        llm.observer = committed_counts
        identities = await SpecialistPlanner(session.deps).advance(session.runtime)

        assert len(identities) == 1
        assert len(llm.plan_inputs) == 2
        assert len(llm.atom_inputs) == 1
        assert llm.observed_before_plan == [(1, 0), (1, 0)]
        assert llm.plan_feedback[0] == ""
        feedback = llm.plan_feedback[1]
        assert FOLLOWUP_CODE in feedback
        assert "$.questions[0].source_result_id" in feedback
        assert "omit" in feedback.lower()
        assert "null" in feedback.lower()
        diagnostic, marker, replay = feedback.partition("\n\nRejected proposal data:\n")
        assert marker
        assert SOURCE_TASK_ID not in diagnostic
        assert SOURCE_RESULT_ID not in diagnostic
        assert json.loads(replay) == {
            "previous_proposal": _plan(explicit_null=True), "omitted": False,
        }

        task_saved = await state.get_task(identities[0])
        assert str(task_saved.source_task_id) == SOURCE_TASK_ID
        assert task_saved.source_result_id == SOURCE_RESULT_ID
        assert len(await state.list_tasks()) == 2
        assert not await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert (await session.status())["usage"]["spent"] == 3
