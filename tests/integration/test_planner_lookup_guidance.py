"""Planner lookup repairs: exact owned codes, no copied input, finite shared budget."""

import json

import pytest
from pydantic import ValidationError

from redteam.orchestration.cognition import _REJECTED_PROPOSAL_MARKER
from redteam.orchestration.planner_context import (
    PlannerContextRequest,
    PlannerEvidenceQuery,
    planner_lookup_diagnostic,
)
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_planner_context_lookup import held, lookup, seed_history
from tests.integration.test_probe_workers import selection
from tests.integration.test_v3_atomization import SplitDouble

MARK = "SECRET-QUERY-MARK"
EXACTLY_ONE = "planner_lookup_needs_exactly_one_query"
READ_ID = "planner_evidence_read_needs_evidence_id"
LIST_ID = "planner_evidence_list_rejects_evidence_id"
BOTH = {"version": 1, "context": {"kind": "task", "mode": "list"}, "evidence": {"mode": "list"}}
CASES = [
    pytest.param({"version": 1}, EXACTLY_ONE, "$.context_request", id="neither"),
    pytest.param(BOTH, EXACTLY_ONE, "$.context_request", id="both"),
    pytest.param({"version": 1, "evidence": {"mode": "read"}}, READ_ID,
                 "$.context_request.evidence", id="read-without-id"),
    pytest.param({"version": 1, "evidence": {"mode": "list", "evidence_id": MARK}}, LIST_ID,
                 "$.context_request.evidence", id="list-with-id"),
]


def bad(context_request):
    return {"reason": "Read the relevant original", "questions": [], "termination": "continue",
            "context_request": context_request}


@pytest.mark.parametrize(("request_body", "code", "location"), CASES)
def test_helper_returns_owned_code_and_location_without_input(request_body, code, location):
    with pytest.raises(ValidationError) as failed:
        PlannerContextRequest.model_validate(request_body)
    diagnostic = planner_lookup_diagnostic(failed.value)
    assert diagnostic is not None
    assert diagnostic.code == code and diagnostic.location == location
    assert diagnostic.validation_issues[0] == {"rule": code, "location": location}
    assert MARK not in diagnostic.model_dump_json()


def test_helper_leaves_other_model_validation_to_its_existing_owner():
    with pytest.raises(ValidationError) as failed:
        PlannerEvidenceQuery.model_validate({"limit": 0})
    assert planner_lookup_diagnostic(failed.value) is None


@pytest.mark.parametrize(("request_body", "code", "location"), CASES)
async def test_invalid_lookup_then_valid_lookup_then_held_uses_finite_budget(
        tmp_path, request_body, code, location):
    llm = SplitDouble([bad(request_body), lookup(kind="task", mode="list", count=1), held()], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 3 and not llm.atom_inputs
        feedback = json.dumps(llm.requests)
        # Empty/mixed shapes now fail the published JSON Schema before the
        # Pydantic owner callback; the typed callback is checked separately above.
        assert ("schema_" if code == EXACTLY_ONE else code) in feedback
        assert location in feedback
        assert "value_error" not in feedback
        # The existing bounded proposal replay may retain a public rejected ID.
        # Code-owned diagnostic feedback must still exclude the proposed value.
        diagnostic_feedback = "\n".join(
            message["content"].split(_REJECTED_PROPOSAL_MARKER, 1)[0]
            for request in llm.requests for message in request.get("messages", [])
        )
        assert MARK not in diagnostic_feedback
        state = session.deps.central_state
        ledger = await state.request_ledger().snapshot(state.run_id)
        assert ledger["spent"] == 3


async def test_unseen_reviewed_reference_is_repaired_by_lookup_then_held(tmp_path):
    cited = {**held(), "reviewed_task_ids": ["z-source"]}
    llm = SplitDouble([cited, lookup(kind="task", mode="list", text="Earlier principal",
                                     count=1), cited], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        await seed_history(session.deps.central_state)
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 3
        feedback = json.dumps(llm.requests)
        assert "planner_reviewed_reference_not_presented at $.reviewed_task_ids" in feedback
        presented = llm.plan_inputs[2]["context_lookup_result"]["result"]["records"]
        assert presented[0]["identity"] == "z-source"
        state = session.deps.central_state
        ledger = await state.request_ledger().snapshot(state.run_id)
        assert ledger["spent"] == 3

