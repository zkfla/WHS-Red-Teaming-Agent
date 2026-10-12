"""Native MODEL -> TOOLS -> SETTLE with central state and original checkpoints."""

from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, START, StateGraph

from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes.react import model_node, settle_node, tools_node
from redteam.orchestration.state import GraphState

NODE_NAMES = ("model", "tools", "settle")


def _after_model(state: GraphState):
    return "settle" if state["runtime"].termination_reason else "tools"


def _central_entry(state: GraphState):
    if state.get("native_calls"):
        return "tools"
    if state["runtime"].phase == AgentPhase.POLICY and state.get("action_proposal") is not None:
        raise ValueError("central_checkpoint_requires_native_execution_continuation")
    return "model"


def _after_settle(state: GraphState):
    return "end" if state["runtime"].is_terminal else "model"


def build_graph(deps: RuntimeDeps, *, checkpointer: Any | None = None) -> Any:
    if deps.central_state is None:
        raise ValueError("runtime_requires_central_state")
    graph = StateGraph(GraphState)

    graph.add_node("model", functools.partial(model_node, deps=deps))
    graph.add_node("tools", functools.partial(tools_node, deps=deps))
    graph.add_node("settle", functools.partial(settle_node, deps=deps))

    graph.add_conditional_edges("model", _after_model, {"tools": "tools", "settle": "settle"})
    graph.add_edge("tools", "settle")
    graph.add_conditional_edges("settle", _after_settle, {"model": "model", "end": END})

    graph.add_conditional_edges(START, _central_entry, {"model": "model", "tools": "tools"})
    return graph.compile(checkpointer=checkpointer)
