"""The native consumer uses the same owned query correction as the planner."""

import json

import pytest

from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.native_local_tools import context_query_handler


@pytest.mark.parametrize(("query", "code", "location"), [
    ({"mode": "related"}, "context_relation_root_required", "$"),
    ({"mode": "read", "refs": [{"kind": "task", "identity": "PRIVATE-MARK\u0000"}]},
     "context_identity_invalid", "$.refs[0].identity"),
    ({"statuses": ["PRIVATE-MARK"]}, "context_status_invalid", "$"),
])
async def test_native_context_query_returns_owned_safe_correction(query, code, location):
    result = await context_query_handler(query, LocalToolContext(deps=None, runtime=None))
    assert not result.ok
    diagnostic = result.payload["diagnostic"]
    assert diagnostic["code"] == code
    assert diagnostic["location"] == location
    assert diagnostic["cause_scope"] == "model_input"
    assert diagnostic["summary"] in result.error
    assert "PRIVATE-MARK" not in json.dumps(result.payload)
    assert "PRIVATE-MARK" not in result.error
