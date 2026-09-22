"""Agent Orchestration. Owned by Agent Team.

LangGraph is the workflow runtime and nothing more. ``RuntimeState`` is
the durable slice — ids and counters, per instruction.md section 5 —
while security domain state lives in the Shared Attack Graph and the
Evidence Store and is referenced by id. ``GraphState`` adds transient
fields so adjacent nodes can hand over in-flight objects within one
turn; they never leave this subpackage.

```text
agents/          who may propose what, and which step to take
nodes/           OBSERVE PLAN POLICY EXECUTE VERIFY UPDATE REPLAN
graph.py         the StateGraph wiring
router.py        phase -> edge
runner.py        driving the loop + checkpoint history / replay
seed.py          planting the engagement scope as the first graph nodes
expectations.py  declarative verification (Verdict, not "success")
settings.py      loop limits and observation size
state.py         RuntimeState / GraphState
```

Docs: ``docs/agent/orchestration.md``.
"""

from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.graph import build_graph
from redteam.orchestration.runner import (
    AgentLoopRunner,
    CheckpointSnapshot,
    ReconciliationResult,
)
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import (
    GraphState,
    RuntimeState,
    TerminationReason,
    initial_graph_state,
)

__all__ = [
    "build_graph",
    "AgentLoopRunner",
    "CheckpointSnapshot",
    "ReconciliationResult",
    "GraphState",
    "RuntimeState",
    "RuntimeDeps",
    "TerminationReason",
    "OrchestrationSettings",
    "initial_graph_state",
    "seed_from_engagement",
]
