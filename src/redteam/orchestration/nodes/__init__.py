"""LangGraph node functions: OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN.

Every node has the same shape — ``(state, *, deps) -> state`` — and each
one owns exactly one responsibility:

```text
observe  schedule an agent, project the graph into a bounded observation
plan     frontier -> planner -> one ActionProposal (+ hypothesis)
policy   hand it to the Control Plane; nothing else
execute  ALLOW-only: build the ToolInvocation and call the gateway
verify   append Evidence, then reach a Verdict against the expectation
update   fold the result back into the Attack Graph
replan   loop again, or stop with a named reason
```

Keep the signature stable — ``redteam.orchestration.graph.build_graph``
binds ``deps`` with ``functools.partial`` and nothing else needs to
change when a node's internals do.
"""

from redteam.orchestration.nodes.execute import execute_node
from redteam.orchestration.nodes.observe import observe_node
from redteam.orchestration.nodes.policy import policy_node
from redteam.orchestration.nodes.replan import replan_node
from redteam.orchestration.nodes.update import update_node
from redteam.orchestration.nodes.verify import verify_node

__all__ = [
    "observe_node",
    "policy_node",
    "execute_node",
    "verify_node",
    "update_node",
    "replan_node",
]
