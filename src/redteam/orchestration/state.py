"""LangGraph-facing state.

``RuntimeState`` is copied verbatim from instruction.md section 5 — the
minimum LangGraph needs, referencing Attack Graph / Evidence data by ID
rather than duplicating it. ``GraphState`` wraps it for LangGraph's
StateGraph: it adds transient (non-persisted) fields so a node can hand
its output object straight to the next node without writing it to the
Attack Graph/Evidence Store first just to read it back. Only
``runtime`` is meaningful outside this subpackage.
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import ActionId, AgentId, EngagementId, HypothesisId, RunId


class RuntimeState(BaseModel):
    run_id: RunId
    engagement_id: EngagementId

    phase: AgentPhase = AgentPhase.OBSERVE
    current_agent: AgentId | None = None

    current_goal: str | None = None

    active_hypothesis_id: HypothesisId | None = None
    pending_action_id: ActionId | None = None

    retry_count: int = 0
    step_count: int = 0

    remaining_budget: int | None = None

    last_verdict: Verdict | None = None


class GraphState(TypedDict, total=False):
    runtime: RuntimeState

    # Transient only — never checkpointed/shared outside this subpackage.
    action_proposal: ActionProposal | None
    policy_decision: PolicyDecision | None
    tool_result: ToolResult | None
