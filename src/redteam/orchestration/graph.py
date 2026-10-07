"""Native ReAct scheduling with durable checkpoints and approval recovery.

Normal turns are MODEL -> TOOLS -> SETTLE -> MODEL/END. Existing policy,
execution and result owners run inside TOOLS. The explicit legacy nodes
remain only to finish approval checkpoints created before this transition.
Domain state remains in the canonical graph, task and evidence stores.
"""

from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, START, StateGraph

from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes import (
    aggregate_node,
    execute_node,
    policy_node,
    update_node,
    verify_node,
    worker_node,
)
from redteam.orchestration.nodes.react import model_node, settle_node, tools_node
from redteam.orchestration.router import route_after_plan, route_after_policy
from redteam.orchestration.state import GraphState

NODE_NAMES = (
    "model",
    "tools",
    "settle",
    "policy",
    "execute",
    "verify",
    "update",
    "worker",
    "aggregate",
)


def _route_entry(state: GraphState):
    """Resume native batches intact; route older approvals to their owner."""
    if state.get("native_calls"):
        return "tools"
    if state["runtime"].phase == AgentPhase.POLICY and state.get("action_proposal") is not None:
        step = state.get("plan_step")
        if step is not None and step.delegation_id and state.get("dispatch_items"):
            return route_after_plan(state)
        return "policy"
    return "model"


def _after_model(state: GraphState):
    return "settle" if state["runtime"].termination_reason else "tools"


def _after_settle(state: GraphState):
    return "end" if state["runtime"].is_terminal else "model"


def build_graph(deps: RuntimeDeps, *, checkpointer: Any | None = None) -> Any:
    graph = StateGraph(GraphState)

    graph.add_node("model", functools.partial(model_node, deps=deps))
    graph.add_node("tools", functools.partial(tools_node, deps=deps))
    graph.add_node("settle", functools.partial(settle_node, deps=deps))

    graph.add_node("policy", functools.partial(policy_node, deps=deps))
    graph.add_node("execute", functools.partial(execute_node, deps=deps))
    graph.add_node("verify", functools.partial(verify_node, deps=deps))
    graph.add_node("update", functools.partial(update_node, deps=deps))
    graph.add_node("worker", functools.partial(worker_node, deps=deps))
    graph.add_node("aggregate", functools.partial(aggregate_node, deps=deps))

    graph.add_conditional_edges(
        START, _route_entry, {"model": "model", "tools": "tools", "policy": "policy"}
    )
    graph.add_conditional_edges("model", _after_model, {"tools": "tools", "settle": "settle"})
    graph.add_edge("tools", "settle")
    graph.add_conditional_edges("settle", _after_settle, {"model": "model", "end": END})
    graph.add_conditional_edges(
        "policy", route_after_policy, {"execute": "execute", "update": "update"}
    )
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", "update")
    graph.add_edge("update", "settle")
    graph.add_edge("worker", "aggregate")
    graph.add_edge("aggregate", "settle")

    return graph.compile(checkpointer=checkpointer)
