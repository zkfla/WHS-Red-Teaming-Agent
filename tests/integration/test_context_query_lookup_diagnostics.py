"""Invalid ContextQuery repairs: typed owned code, one valid lookup, held within 3 requests."""

import json

import pytest

from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_planner_context_lookup import held, lookup
from tests.integration.test_probe_workers import selection
from tests.integration.test_v3_atomization import SplitDouble

MARK = "SECRET-CONTEXT-MARK"
CASES = [
    pytest.param({"mode": "related"}, "context_relation_root_required",
                 "$.context_request.context", id="related-no-root"),
    pytest.param({"mode": "list", "refs": [{"kind": "task", "identity": "z-source"}]},
                 "context_refs_require_related_or_read", "$.context_request.context",
                 id="list-with-root"),
    pytest.param({"mode": "read", "section": "properties",
                  "refs": [{"kind": "task", "identity": "z-source"}]},
                 "context_graph_section_required", "$.context_request.context",
                 id="graph-section-on-task"),
    pytest.param({"mode": "list", "statuses": [MARK]}, "context_status_invalid",
                 "$.context_request.context", id="status-unknown"),
    pytest.param({"mode": "related", "refs": [{"kind": "task", "identity": MARK + "\x00"}]},
                 "context_identity_invalid", "$.context_request.context.refs[0].identity",
                 id="identity-control"),
]


def bad(context):
    return {"reason": "Read the relevant original", "questions": [], "termination": "continue",
            "context_request": {"version": 1, "context": context}}


@pytest.mark.parametrize(("context", "code", "location"), CASES)
async def test_invalid_context_query_then_valid_lookup_then_held_uses_finite_budget(
        tmp_path, context, code, location):
    llm = SplitDouble([bad(context), lookup(kind="task", mode="list", count=1), held()], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 3 and not llm.atom_inputs
        feedback = json.dumps(llm.requests)
        assert f"{code} at {location}" in feedback
        assert "value_error" not in feedback and MARK not in feedback
        state = session.deps.central_state
        ledger = await state.request_ledger().snapshot(state.run_id)
        assert ledger["spent"] == 3
