"""Conditional-edge routing functions for the LangGraph graph."""

from __future__ import annotations

from redteam.core.enums import AgentPhase
from redteam.orchestration.state import GraphState


def route_after_policy(state: GraphState) -> str:
    """POLICY -> EXECUTE only on ALLOW; otherwise straight to REPLAN.

    This is what structurally prevents a denied/pending action from
    ever reaching the Capability Layer.
    """
    return "execute" if state["runtime"].phase == AgentPhase.EXECUTE else "replan"
