"""Builds the LangGraph ``StateGraph`` for a single agent run.

```text
START -> OBSERVE -> PLAN -> POLICY --ALLOW--> EXECUTE -> VERIFY -> UPDATE -> REPLAN -> END
                              \\--deny/require_approval--> REPLAN --^
```

Kept intentionally linear/single-pass for this stage (instruction.md
section 14: "초기 Graph는 아주 단순하게"). ``replan_node`` always ends
the run today; looping back to ``observe`` is a TODO for Agent Team.
"""

from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, StateGraph

from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes import (
    execute_node,
    observe_node,
    plan_node,
    policy_node,
    replan_node,
    update_node,
    verify_node,
)
from redteam.orchestration.router import route_after_policy
from redteam.orchestration.state import GraphState


def build_graph(deps: RuntimeDeps) -> Any:
    graph = StateGraph(GraphState)

    graph.add_node("observe", functools.partial(observe_node, deps=deps))
    graph.add_node("plan", functools.partial(plan_node, deps=deps))
    graph.add_node("policy", functools.partial(policy_node, deps=deps))
    graph.add_node("execute", functools.partial(execute_node, deps=deps))
    graph.add_node("verify", functools.partial(verify_node, deps=deps))
    graph.add_node("update", functools.partial(update_node, deps=deps))
    graph.add_node("replan", functools.partial(replan_node, deps=deps))

    graph.set_entry_point("observe")
    graph.add_edge("observe", "plan")
    graph.add_edge("plan", "policy")
    graph.add_conditional_edges(
        "policy", route_after_policy, {"execute": "execute", "replan": "replan"}
    )
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", "update")
    graph.add_edge("update", "replan")
    graph.add_edge("replan", END)

    return graph.compile()
