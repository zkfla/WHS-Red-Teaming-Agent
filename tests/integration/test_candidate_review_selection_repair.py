"""Wrong Task completion receipt on a candidate verdict, repaired through the real planner.

The default split profile and the legacy single judgment both run through the production
planner loop. Only model answers are scripted. The observation, final public review receipt,
canonical admission and budget are real. Each judgment's first attempt cites a real Task
completion receipt and must be repaired before any atomizer call or state write.
"""

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.core.ids import EvidenceId
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_candidate_subject_repair import _atomize
from tests.integration.test_central_development import TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, held, probe
from tests.integration.test_hypothesis_planner import selection as legacy_selection
from tests.integration.test_observation_candidate_chain_06d import (
    MeaningLLM,
    _graph_record,
    _meaning,
    _observe_candidate,
    _receipt,
)
from tests.integration.test_probe_workers import ReviewedObservationModel, drive, register
from tests.integration.test_probe_workers import selection as split_selection

REPAIR_MARKER = "\n\nPrevious attempt failed"


class RepairLLM(ReviewedObservationModel):
    """Scripted planner answers in either shape; observation and review keep production paths."""

    def __init__(self, plans, atoms=()):
        super().__init__()
        self.plans = list(plans)
        self.atoms = list(atoms)
        self.plan_inputs = []
        self.plan_feedback = []
        self.atom_inputs = []
        self.observer = None
        self.observed = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "updates" in properties:
            body, _, feedback = messages[-1]["content"].partition(REPAIR_MARKER)
            payload = json.loads(body)
            self.plan_inputs.append(payload)
            self.plan_feedback.append(feedback)
            if self.observer is not None:
                self.observed.append(await self.observer())
            response = self.plans.pop(0)
            return response(payload) if callable(response) else json.loads(json.dumps(response))
        if "tasks" in properties:
            body = messages[-1]["content"].partition(REPAIR_MARKER)[0]
            payload = json.loads(body)
            self.atom_inputs.append(payload)
            return self.atoms.pop(0)(payload)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


def _opening(mode):
    if mode == "legacy":
        return {"updates": [candidate()], "tasks": [probe(candidate="local")],
                "reason": "Observe the candidate's smallest uncertainty"}
    return {"updates": [candidate()], "questions": [{
        "question_id": "q1",
        "objective": "Inspect the candidate premise",
        "target_node_ids": ["host:unit.local"],
        "candidate_refs": ["local"],
        "completion_criteria": [{"kind": "observation",
                                 "description": "Inspect the candidate response"}],
    }], "reason": "One scoped observation can resolve the candidate uncertainty",
        "termination": "continue"}


def _judgment(mode, update, *, reason):
    body = {"updates": [update], "termination": "replan", "reason": reason}
    if mode == "split":
        body["questions"] = []
    return body


def _hold(mode):
    body = held("Await an independent review of the retained original")
    if mode == "split":
        body["questions"] = []
    return body


@pytest.mark.parametrize("correction", ["receipt", "held"])
@pytest.mark.parametrize("mode", ["split", "legacy"])
async def test_wrong_task_receipt_is_repaired_before_atomizer_or_writes(
    tmp_path, mode, correction,
):
    llm = RepairLLM([_opening(mode)], atoms=[_atomize] if mode == "split" else [])
    open_session = split_selection if mode == "split" else legacy_selection
    async with open_session(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)

        async def snapshot():
            (current,) = await service.list_hypotheses()
            return (len(await state.list_tasks()), current.revision, current.status,
                    len(await service.list_findings()))

        # Real observation Task, real graph.record review receipt, scripted meaning answer.
        after_task, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success")]), session.deps.evidence_store, service,
        )
        public = await _receipt(session, await _graph_record(
            session, verifier, candidate_id=after_task.hypothesis_id, source=source,
        ))
        assert public.metadata["record_kind"] == "review_receipt"

        # A separate settled Task produces the real Task completion receipt the model may cite.
        reviewed = await register(session, "reviewed", actions=2)
        llm.native_responses_by_task[reviewed] = [discover(TOOL), turn(inspect("reviewed"))]
        (settled,) = await drive(session)
        assert settled.kind == "completed", settled.error
        completion_id = (await state.get_task(reviewed)).result.completion_receipt_id
        completion = await session.deps.evidence_store.get(EvidenceId(completion_id))
        assert completion is not None
        assert completion.metadata["record_kind"] == "task_completion_receipt"

        cited = [str(source.evidence_id)]
        wrong = candidate(current=after_task.model_dump(mode="json"), status="confirmed",
                          evidence=cited, review=completion_id)
        good = candidate(current=after_task.model_dump(mode="json"), status="confirmed",
                         evidence=cited, review=str(public.evidence_id))
        reason = "The bound independent review supports this candidate verdict"
        llm.observer = snapshot
        llm.plans.extend([
            _judgment(mode, wrong, reason=reason),
            _judgment(mode, good, reason=reason) if correction == "receipt" else _hold(mode),
        ])
        atoms_before = len(llm.atom_inputs)
        spent_before = (await session.status())["usage"]["spent"]

        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []

        # The wrong selection never reaches atomization, and it writes nothing before repair.
        assert len(llm.atom_inputs) == atoms_before
        assert len(llm.observed) == 2 and llm.observed[0] == llm.observed[1]
        assert len(llm.plan_inputs) == 3 and llm.plan_feedback[1] == ""
        feedback = llm.plan_feedback[2]
        assert "candidate_review_receipt_invalid" in feedback
        assert "$.updates[0].review_evidence_id" in feedback
        assert completion_id not in feedback
        # One wrong and one corrected or held judgment, both on the shared finite budget.
        assert (await session.status())["usage"]["spent"] - spent_before == 2

        saved = await service.get_hypothesis(after_task.hypothesis_id)
        assert await service.list_findings() == []
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
        if correction == "receipt":
            # The corrected public review passes through the original canonical gate.
            assert saved.status == "confirmed" and saved.revision == 2
            assert saved.metadata["candidate_review_evidence_id"] == str(public.evidence_id)
        else:
            assert saved.status == "open" and saved.revision == 1
            assert "candidate_review_evidence_id" not in saved.metadata
