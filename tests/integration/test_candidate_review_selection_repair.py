"""Only a final public candidate-review receipt can drive direct application.

The observation, review receipt, Task completion receipt and Central State application are real.
Only the independent meaning answer is scripted. A Task completion receipt must neither replace
the final review nor duplicate the candidate/Finding already committed from that review.
"""

import json

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.core.ids import EvidenceId
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.candidate_review_application import apply_candidate_review_receipt
from tests.integration.test_central_development import TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate, probe, selection
from tests.integration.test_observation_candidate_chain_06d import (
    ChainLLM,
    MeaningLLM,
    _graph_record,
    _meaning,
    _observe_candidate,
    _receipt,
)
from tests.integration.test_probe_workers import ReviewedObservationModel, drive, register

REPAIR_MARKER = "\n\nPrevious attempt failed"


class RepairLLM(ReviewedObservationModel):
    """Shared planner fake for the remaining invalid-selection repair tests."""

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
            messages=messages,
            response_schema=response_schema,
            system=system,
            **kwargs,
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


async def test_task_completion_receipt_cannot_replace_applied_candidate_review(tmp_path):
    opening = {"updates": [candidate()], "tasks": [probe(candidate="local")],
               "reason": "Observe the candidate's smallest uncertainty"}
    async with selection(tmp_path, llm=ChainLLM([opening])) as (session, llm, tools):
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)

        current, source = await _observe_candidate(session, llm)
        verifier = StructuredLLMAnalysisVerifier(
            MeaningLLM([_meaning("success")]), session.deps.evidence_store, service,
        )
        public = await _receipt(session, await _graph_record(
            session, verifier, candidate_id=current.hypothesis_id, source=source,
        ))
        assert public.metadata["record_kind"] == "review_receipt"
        promoted = await service.get_hypothesis(current.hypothesis_id)
        assert promoted.status == "confirmed" and promoted.revision == 2
        finding, = await service.list_findings()

        reviewed = await register(session, "reviewed", actions=2)
        llm.native_responses_by_task[reviewed] = [discover(TOOL), turn(inspect("reviewed"))]
        settled, = await drive(session)
        assert settled.kind == "completed", settled.error
        completion_id = (await state.get_task(reviewed)).result.completion_receipt_id
        completion = await session.deps.evidence_store.get(EvidenceId(completion_id))
        assert completion.metadata["record_kind"] == "task_completion_receipt"

        wrong = await apply_candidate_review_receipt(session.deps, str(completion_id))
        assert wrong.status == "refused"
        assert wrong.reason == "candidate_review_receipt_kind_invalid"
        replay = await apply_candidate_review_receipt(
            session.deps,
            str(public.evidence_id),
        )
        assert replay.status == "already_applied"
        unchanged = await service.get_hypothesis(current.hypothesis_id)
        assert unchanged.status == "confirmed" and unchanged.revision == 2
        assert [item.finding_id for item in await service.list_findings()] == [finding.finding_id]
        assert len(await state.candidate_finding_promotions()) == 1
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
