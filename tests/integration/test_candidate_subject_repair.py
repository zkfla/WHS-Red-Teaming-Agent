"""Stage-1 candidate subject repair through the real planner loop (scripted provider only).

The first stage-1 judgment names a registered second host that the new candidate does not
cover. The planner must reject it with a typed, field-precise diagnostic, let the repair
correct it, and run the atomizer exactly once on the corrected judgment.
"""

import json

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import GraphNodeType
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.seed import SCOPE_LABEL
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import selection as probe_selection

SUBJECT_CODE = "plan_question_target_outside_candidate"
FEEDBACK_MARKER = "Previous attempt failed"


class ScriptedPlannerLLM(FakeLLMClient):
    """Scripted stage-1 and stage-2 responses; the production client and loop run unchanged."""

    def __init__(self, plans, atoms):
        super().__init__()
        self.plans = list(plans)
        self.atoms = list(atoms)
        self.plan_inputs = []
        self.plan_feedback = []
        self.atom_inputs = []
        self.observed_before_plan = []
        self.observer = None

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "questions" in properties:
            prompt = messages[-1]["content"]
            body, _, feedback = prompt.partition("\n\n" + FEEDBACK_MARKER)
            self.plan_inputs.append(json.loads(body))
            self.plan_feedback.append(feedback)
            if self.observer is not None:
                self.observed_before_plan.append(await self.observer())
            await self._wait_response("complete_structured", messages)
            return json.loads(json.dumps(self.plans.pop(0)))
        if "tasks" in properties:
            body = messages[-1]["content"].split("\n\n" + FEEDBACK_MARKER)[0]
            payload = json.loads(body)
            self.atom_inputs.append(payload)
            await self._wait_response("complete_structured", messages)
            return self.atoms.pop(0)(payload)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


def _plan(question_targets):
    return {
        "updates": [{
            "expected_revision": None,
            "hypothesis": {
                "contract_version": 2,
                "hypothesis_id": "local-candidate",
                "description": "Access may be unguarded",
                "target_node_ids": [TARGET],
                "reason": "Assess the preserved response",
            },
        }],
        "questions": [{
            "question_id": "q1",
            "objective": "Inspect the candidate premise",
            "target_node_ids": question_targets,
            "candidate_refs": ["local-candidate"],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the candidate response",
            }],
        }],
        "reason": "One scoped observation can resolve the candidate uncertainty",
        "termination": "continue",
    }


def _atomize(payload):
    question, = payload["questions"]
    return {"tasks": [{
        "task_key": "candidate-probe",
        "question_id": "q1",
        "objective": "Inspect the candidate premise",
        "title": "Candidate response probe",
        "role_id": "probe-worker",
        "target_node_ids": [TARGET],
        "completion_criterion_ids": [item["criterion_id"]
                                     for item in question["completion_criteria"]],
        "probe_limits": {"max_actions": 1, "max_model_requests": 4},
    }]}


async def _add_second_in_scope_host(session):
    node = await AttackGraphService(session.deps.graph_store).upsert_node(
        GraphNodeType.HOST, key="second.local", attrs={"in_scope": True},
        facts={"target_ref": "second.local"}, labels=[SCOPE_LABEL],
        provenance=Provenance(
            step=0, source="engagement:source-eng", run_id=str(session.runtime.run_id),
            confidence=Confidence.SERVER_DECLARED,
        ),
        step=0,
    )
    return str(node.id)


async def test_scope_mismatch_repairs_once_then_atomizes_once(tmp_path):
    llm = ScriptedPlannerLLM(plans=[], atoms=[_atomize])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        extra = await _add_second_in_scope_host(session)
        state = session.deps.central_state
        graph = AttackGraphService(session.deps.graph_store)

        async def committed_counts():
            return len(await state.list_tasks()), len(await graph.list_hypotheses())

        llm.observer = committed_counts
        llm.plans = [_plan([TARGET, extra]), _plan([TARGET])]

        ids = await SpecialistPlanner(session.deps).advance(session.runtime)

        assert len(ids) == 1
        assert len(llm.plan_inputs) == 2
        assert len(llm.atom_inputs) == 1
        assert llm.observed_before_plan == [(0, 0), (0, 0)]
        assert llm.atom_inputs[0]["questions"][0]["target_node_ids"] == [TARGET]

        feedback = llm.plan_feedback[1]
        assert SUBJECT_CODE in feedback
        assert "$.questions[0].target_node_ids" in feedback
        assert extra not in feedback
        assert llm.plan_feedback[0] == ""

        assert (await session.status())["usage"]["spent"] == 3
        (candidate,) = await graph.list_hypotheses()
        assert candidate.revision == 1
        task = await state.get_task(ids[0])
        assert task.hypothesis_revisions == {candidate.hypothesis_id: 1}

