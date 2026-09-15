"""Checkpoint serialization: a restored RuntimeState is still a RuntimeState."""

from __future__ import annotations

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import AgentId, EngagementId, RunId
from redteam.orchestration.checkpointing import build_checkpointer, state_type_allowlist
from redteam.orchestration.state import RuntimeState, initial_graph_state

REQUIRED = [
    ("redteam.orchestration.state", "RuntimeState"),
    ("redteam.orchestration.agents.base", "PlanStep"),
    ("redteam.attack_graph.observation", "GraphObservation"),
    ("redteam.core.contracts.action", "ActionProposal"),
    ("redteam.core.contracts.policy", "PolicyDecision"),
    ("redteam.core.contracts.tool", "ToolResult"),
    ("redteam.core.enums", "AgentPhase"),
    ("redteam.core.enums", "Verdict"),
]


def test_every_type_that_can_appear_in_graph_state_is_allowlisted() -> None:
    allowlist = set(state_type_allowlist())
    missing = [entry for entry in REQUIRED if entry not in allowlist]
    assert not missing, f"not allowlisted, so these would restore as raw dicts: {missing}"


def test_the_allowlist_stays_inside_agent_team_and_core_packages() -> None:
    # Nothing from control/capability/evidence belongs in workflow state,
    # and reaching into them from here would cross a team boundary.
    forbidden = ("redteam.control", "redteam.capability", "redteam.evidence")
    assert not [
        module for module, _ in state_type_allowlist() if module.startswith(forbidden)
    ]


def test_a_round_tripped_runtime_state_keeps_its_type_and_values() -> None:
    # Without the allowlist these come back as plain dicts, and the
    # failure is silent until something reads `.phase`.
    checkpointer = build_checkpointer(enabled=True)
    assert checkpointer is not None
    state = initial_graph_state(
        RuntimeState(
            run_id=RunId("run_1"),
            engagement_id=EngagementId("eng_1"),
            phase=AgentPhase.VERIFY,
            current_agent=AgentId("recon-agent"),
            last_verdict=Verdict.VIOLATION,
            step_count=3,
            finished_agents=[AgentId("recon-agent")],
        )
    )
    state["observation"] = GraphObservation(step=3, node_counts={"host": 2})

    restored = checkpointer.serde.loads_typed(checkpointer.serde.dumps_typed(state))

    runtime = restored["runtime"]
    assert isinstance(runtime, RuntimeState)
    assert runtime.phase is AgentPhase.VERIFY
    assert runtime.last_verdict is Verdict.VIOLATION
    assert runtime.finished_agents == [AgentId("recon-agent")]
    assert isinstance(restored["observation"], GraphObservation)
    assert restored["observation"].node_counts == {"host": 2}


def test_checkpointing_can_be_turned_off() -> None:
    assert build_checkpointer(enabled=False) is None
