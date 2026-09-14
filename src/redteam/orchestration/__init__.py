"""Agent Orchestration. Owned by Agent Team.

LangGraph is used only as workflow runtime — ``state.RuntimeState`` is
the durable, loggable slice of state (IDs only, per instruction.md
section 5); ``state.GraphState`` is the transient LangGraph working
state used to shuttle in-flight objects (the current ``ActionProposal``,
``ToolResult``) one hop between adjacent nodes in the same run. It is
never persisted or exposed outside this subpackage.
"""

from redteam.orchestration.graph import build_graph
from redteam.orchestration.state import GraphState, RuntimeState

__all__ = ["build_graph", "GraphState", "RuntimeState"]
