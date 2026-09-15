"""Conditional-edge routing: ``RuntimeState.phase`` -> edge name.

The decision is made by the node that has the context; these functions
only translate it, which keeps the ALLOW gate to one readable expression.
"""

from __future__ import annotations

from redteam.core.enums import AgentPhase
from redteam.orchestration.state import GraphState

EXECUTE = "execute"
UPDATE = "update"
REPLAN = "replan"
PLAN = "plan"
POLICY = "policy"
OBSERVE = "observe"
END = "end"


def route_after_observe(state: GraphState) -> str:
    """No agent left to schedule: skip planning and let REPLAN end the run."""
    return REPLAN if state["runtime"].phase == AgentPhase.REPLAN else PLAN


def route_after_plan(state: GraphState) -> str:
    """No proposal (empty frontier / planner declined) skips straight to REPLAN."""
    return POLICY if state.get("action_proposal") is not None else REPLAN


def route_after_policy(state: GraphState) -> str:
    """POLICY -> EXECUTE only on ALLOW.

    Non-ALLOW goes to UPDATE — not to run anything, but to record in the
    graph that the action was blocked.
    """
    return EXECUTE if state["runtime"].phase == AgentPhase.EXECUTE else UPDATE


def route_after_replan(state: GraphState) -> str:
    """Loop back for another turn, or end the run."""
    return END if state["runtime"].is_terminal else OBSERVE
