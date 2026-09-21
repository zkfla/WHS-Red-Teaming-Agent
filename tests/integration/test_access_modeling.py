"""#2: a session that reaches an endpoint is modelled as a Principal and a
CAN_ACCESS edge (Object/Permission), so who-can-reach-what is first-class in
the graph rather than buried in an observation blob.
"""

from __future__ import annotations

from redteam.attack_graph.service import AttackGraphService
from tests.integration.test_authentication_followup import exercise


async def test_session_access_becomes_a_principal_and_can_access_edge() -> None:
    # No expected_access declared -> benign "session_access_observed" (200),
    # not a Finding, but still a real access relationship worth recording.
    _, deps, _, _ = await exercise(
        sessions=[{"session_ref": "alice", "paths": ["/"]}],
        authenticated=200,
    )
    graph = AttackGraphService(deps.graph_store)

    principals = [n for n in await graph.list_nodes() if n.type.value == "principal"]
    assert any("alice" in str(p.id) for p in principals), "the session became a Principal"

    can_access = [e for e in await graph.list_edges() if e.type.value == "can_access"]
    assert can_access, "the reached endpoint must be linked as CAN_ACCESS"
    assert can_access[0].properties.get("outcome") == "session_access_observed"
    # No verified Finding for an undeclared session simply having access.
    assert await graph.list_findings() == []
