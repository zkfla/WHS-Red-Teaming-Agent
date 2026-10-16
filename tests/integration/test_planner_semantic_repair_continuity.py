"""A scripted provider corrects its rejected proposal through the real planner path."""

import json

from redteam.attack_graph.service import AttackGraphService
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_candidate_subject_repair import (
    SUBJECT_CODE,
    ScriptedPlannerLLM,
    _add_second_in_scope_host,
    _atomize,
    _plan,
)
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import selection as probe_selection

REPLAY_MARKER = "\n\nRejected proposal data:\n"
_REQUEST_CONTEXT_FIELDS = frozenset({"model_usage", "decision_budget", "remaining_budget"})


def _captured_planner_inputs(payload):
    """Remove only telemetry sampled for each actual attempt, not captured context."""
    return {key: value for key, value in payload.items()
            if key not in _REQUEST_CONTEXT_FIELDS}


class CorrectingPlannerLLM(ScriptedPlannerLLM):
    """Repair only the rejected subject, retaining proposal-local references from the wire."""

    def __init__(self):
        super().__init__(plans=[], atoms=[_atomize])
        self.replayed = []
        self.corrected = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "questions" in response_schema.get("properties", {}) and self.plan_inputs:
            prompt = messages[-1]["content"]
            diagnostic, marker, encoded = prompt.partition(REPLAY_MARKER)
            assert marker, "semantic correction must retain the rejected structured proposal"
            assert SUBJECT_CODE in diagnostic
            envelope = json.loads(encoded)
            assert envelope["omitted"] is False
            proposal = envelope["previous_proposal"]
            self.replayed.append(json.loads(json.dumps(proposal)))
            # The provider receives this plan only through its current request. It needs no
            # private transcript or hard-coded reconstruction of local question/candidate IDs.
            proposal["questions"][0]["target_node_ids"] = list(
                proposal["updates"][0]["hypothesis"]["target_node_ids"]
            )
            self.corrected.append(json.loads(json.dumps(proposal)))
            self.plans.append(proposal)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


async def test_rejected_local_references_survive_correction_before_atomic_commit(tmp_path):
    llm = CorrectingPlannerLLM()
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        extra = await _add_second_in_scope_host(session)
        graph = AttackGraphService(session.deps.graph_store)
        state = session.deps.central_state

        async def committed_counts():
            return len(await state.list_tasks()), len(await graph.list_hypotheses())

        llm.observer = committed_counts
        rejected = _plan([TARGET, extra])
        # The split planner schema requires the candidate's current status even
        # though the shared helper also serves older single-stage fixtures.
        rejected["updates"][0]["hypothesis"]["status"] = "open"
        rejected["updates"][0]["hypothesis"]["hypothesis_id"] = "proposal-local-candidate-73"
        rejected["questions"][0]["candidate_refs"] = ["proposal-local-candidate-73"]
        # Atomizer helper expects q1, so vary the candidate's proposal-local key only.
        llm.plans = [rejected]

        task_ids = await SpecialistPlanner(session.deps).advance(session.runtime)

        assert len(task_ids) == 1
        assert llm.replayed == [rejected]
        assert len(llm.plan_inputs) == 2
        first, repaired = llm.plan_inputs
        assert _captured_planner_inputs(first) == _captured_planner_inputs(repaired)
        assert first["model_usage"]["spent"] == 0
        assert repaired["model_usage"]["spent"] == 1
        assert first["model_usage"]["unknown_usage"] == 0
        assert repaired["model_usage"]["unknown_usage"] == 1
        assert first["decision_budget"]["used"] == 1
        assert repaired["decision_budget"]["used"] == 2
        assert first["decision_budget"]["normal_used"] == 1
        assert first["decision_budget"]["repair_used"] == 0
        assert repaired["decision_budget"]["normal_used"] == 1
        assert repaired["decision_budget"]["repair_used"] == 1
        assert llm.observed_before_plan == [(0, 0), (0, 0)]
        assert len(llm.atom_inputs) == 1
        assert llm.replayed[0]["updates"][0]["hypothesis"]["hypothesis_id"] == (
            "proposal-local-candidate-73"
        )
        assert llm.corrected[0]["questions"][0]["candidate_refs"] == [
            "proposal-local-candidate-73"
        ]
        assert llm.atom_inputs[0]["questions"][0]["target_node_ids"] == [TARGET]
        assert (await session.status())["usage"]["spent"] == 3
        (candidate,) = await graph.list_hypotheses()
        assert candidate.revision == 1
        task = await state.get_task(task_ids[0])
        assert task.hypothesis_revisions == {candidate.hypothesis_id: 1}
