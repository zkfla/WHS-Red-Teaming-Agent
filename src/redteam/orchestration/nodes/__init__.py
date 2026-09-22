"""LangGraph node functions: OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN.

Every real node here is a stub/fake per instruction.md section 14 — they
exist to prove the architecture wiring works end to end, not to
implement real recon/exploitation logic. Agent Team replaces the body
of ``plan_node`` (and eventually adds real branching in ``replan_node``)
with actual LLM-driven planning; the node signature
``(state, *, deps) -> state`` should stay stable so
``redteam.orchestration.graph`` doesn't need to change shape.
"""

from redteam.orchestration.nodes.execute import execute_node
from redteam.orchestration.nodes.observe import observe_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.policy import policy_node
from redteam.orchestration.nodes.replan import replan_node
from redteam.orchestration.nodes.update import update_node
from redteam.orchestration.nodes.verify import verify_node

__all__ = [
    "observe_node",
    "plan_node",
    "policy_node",
    "execute_node",
    "verify_node",
    "update_node",
    "replan_node",
]
