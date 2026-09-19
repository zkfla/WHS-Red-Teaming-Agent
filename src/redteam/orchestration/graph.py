"""The LangGraph ``StateGraph`` for one agent run. Owned by Agent Team.

```text
START -> OBSERVE -> PLAN --proposal--> POLICY --ALLOW--> EXECUTE -> VERIFY --+
            ^         |                   |                                 |
            |         |                   \\--deny/approval-needed--> UPDATE <+
            |         |                                                 |
            |         \\--no proposal--> REPLAN <------------------------+
            |                              |
            \\-------- another turn -------+-- terminal --> END
```

LangGraph is the workflow runtime and nothing more: it schedules these
seven functions and persists ``GraphState`` between them. It owns no
domain model — the Attack Graph and the Evidence Store do, and
``RuntimeState`` only carries their ids. Replacing LangGraph would mean
rewriting this file and ``runner.py``; no node, contract or other team's
module would change.

``build_graph(deps)`` still returns a compiled graph with the same
signature the skeleton had. Pass ``checkpointer=`` to get per-super-step
snapshots (see ``checkpointing.py``); ``runner.AgentLoopRunner`` wires
that plus the recursion limit the loop needs.
"""

from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, StateGraph

from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes import (
    aggregate_node,
    execute_node,
    observe_node,
    plan_node,
    policy_node,
    replan_node,
    update_node,
    verify_node,
    worker_node,
)
from redteam.orchestration.router import (
    route_after_observe,
    route_after_plan,
    route_after_policy,
    route_after_replan,
)
from redteam.orchestration.state import GraphState

NODE_NAMES = (
    "observe",
    "plan",
    "policy",
    "execute",
    "verify",
    "update",
    "worker",
    "aggregate",
    "replan",
)


def build_graph(deps: RuntimeDeps, *, checkpointer: Any | None = None) -> Any:
    graph = StateGraph(GraphState)

    graph.add_node("observe", functools.partial(observe_node, deps=deps))
    graph.add_node("plan", functools.partial(plan_node, deps=deps))
    graph.add_node("policy", functools.partial(policy_node, deps=deps))
    graph.add_node("execute", functools.partial(execute_node, deps=deps))
    graph.add_node("verify", functools.partial(verify_node, deps=deps))
    graph.add_node("update", functools.partial(update_node, deps=deps))
    graph.add_node("worker", functools.partial(worker_node, deps=deps))
    graph.add_node("aggregate", functools.partial(aggregate_node, deps=deps))
    graph.add_node("replan", functools.partial(replan_node, deps=deps))

    graph.set_entry_point("observe")
    graph.add_conditional_edges(
        "observe", route_after_observe, {"plan": "plan", "replan": "replan"}
    )
    graph.add_conditional_edges(
        "plan", route_after_plan, {"policy": "policy", "replan": "replan"}
    )
    graph.add_conditional_edges(
        "policy", route_after_policy, {"execute": "execute", "update": "update"}
    )
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", "update")
    graph.add_edge("update", "replan")
    graph.add_edge("worker", "aggregate")
    graph.add_edge("aggregate", "replan")
    graph.add_conditional_edges(
        "replan", route_after_replan, {"observe": "observe", "end": END}
    )

    return graph.compile(checkpointer=checkpointer)
