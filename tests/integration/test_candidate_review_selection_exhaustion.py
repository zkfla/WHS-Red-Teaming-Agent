"""Repeated Planner attempts to own final review exhaust the finite repair budget.

The real split and legacy planners run the production validation loop. Only model answers are
scripted. Every judgment cites a real Task completion receipt as the candidate review, so each
attempt is rejected before atomization or any state write. Exhaustion must stop at the configured
request count, leave the authoritative state unchanged, and report only the indexed location.
"""

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_candidate_review_selection_repair import RepairLLM, _judgment, _opening
from tests.integration.test_candidate_subject_repair import _atomize
from tests.integration.test_central_development import TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate
from tests.integration.test_hypothesis_planner import selection as legacy_selection
from tests.integration.test_observation_candidate_chain_06d import (
    MeaningLLM,
    _graph_record,
    _meaning,
    _observe_candidate,
    _receipt,
)
from tests.integration.test_probe_workers import drive, register
from tests.integration.test_probe_workers import selection as split_selection

REASON = "The bound independent review supports this candidate verdict"
LOCATION = "$.updates[0].review_evidence_id"
OWNERSHIP_CODE = "planner_review_application_owned_by_code"


@pytest.mark.parametrize("mode", ["split", "legacy"])
async def test_repeated_wrong_review_selection_exhausts_repair_budget_without_writes(
    tmp_path, mode,
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
            MeaningLLM([_meaning("unknown")]), session.deps.evidence_store, service,
        )
        public = await _receipt(session, await _graph_record(
            session, verifier, candidate_id=after_task.hypothesis_id, source=source,
        ))
        assert public.metadata["record_kind"] == "review_receipt"

        # A separate settled Task produces the real completion receipt the model keeps citing.
        reviewed = await register(session, "reviewed", actions=2)
        llm.native_responses_by_task[reviewed] = [discover(TOOL), turn(inspect("reviewed"))]
        (settled,) = await drive(session)
        assert settled.kind == "completed", settled.error
        completion_id = (await state.get_task(reviewed)).result.completion_receipt_id

        wrong = candidate(current=after_task.model_dump(mode="json"), status="confirmed",
                          evidence=[str(source.evidence_id)], review=completion_id)
        repair_limit = session.deps.settings.decision_max_attempts
        # One normal request and every repair fill the budget; one extra answer stays unused.
        llm.observer = snapshot
        llm.plans.extend(_judgment(mode, wrong, reason=REASON) for _ in range(repair_limit + 2))
        before = await snapshot()
        plans_before = len(llm.plan_inputs)
        atoms_before = len(llm.atom_inputs)
        spent_before = (await session.status())["usage"]["spent"]

        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)

        diagnostic = failed.value.diagnostic
        assert diagnostic.code == "decision_repair_budget_exhausted"
        assert diagnostic.stage == "hypothesis_planning" and diagnostic.kind == "budget"
        assert diagnostic.repair_limit == repair_limit
        assert diagnostic.attempts == 1 + repair_limit
        assert diagnostic.last_cause is not None
        assert diagnostic.last_cause.code == OWNERSHIP_CODE
        assert diagnostic.last_cause.location == LOCATION
        assert completion_id not in diagnostic.model_dump_json()
        assert completion_id not in str(failed.value)

        # Exactly one normal request and the bounded repairs reached the model, nothing more.
        assert len(llm.plan_inputs) - plans_before == 1 + repair_limit
        assert len(llm.plans) == 1
        assert len(llm.observed) == 1 + repair_limit
        assert all(snapshot_at_call == before for snapshot_at_call in llm.observed)
        assert (await session.status())["usage"]["spent"] - spent_before == 1 + repair_limit
        feedback = llm.plan_feedback[plans_before:]
        assert len(feedback) == 1 + repair_limit and feedback[0] == ""
        assert all(OWNERSHIP_CODE in item and LOCATION in item
                   for item in feedback[1:])
        for item in feedback[1:]:
            diagnostic_text, marker, replay = item.partition("\n\nRejected proposal data:\n")
            assert marker and completion_id not in diagnostic_text
            assert completion_id in replay

        # No atomizer ran on any rejection, and no candidate, Task or Finding was written.
        assert len(llm.atom_inputs) == atoms_before
        assert await snapshot() == before
        saved = await service.get_hypothesis(after_task.hypothesis_id)
        assert saved.status == "open" and saved.revision == 1
        assert "candidate_review_evidence_id" not in saved.metadata
        assert await service.list_findings() == []
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
