"""ActionProposal — the only way an Agent may ask for a Tool to run.

Agents never call the Capability Layer directly. They emit an
``ActionProposal`` and hand it, with a ``ControlContext``, to the
Control Plane (``redteam.core.ports.control_plane.ControlPlane``); only
an ALLOW decision may become a ``ToolInvocation`` (see
``redteam.core.convert.build_tool_invocation``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.contracts.target import TargetRef
from redteam.core.ids import ActionId, AgentId, EngagementId, HypothesisId, RunId


class ActionProposal(BaseModel):
    action_id: ActionId
    run_id: RunId
    engagement_id: EngagementId
    agent_id: AgentId

    capability: str
    operation: str

    target: TargetRef

    # Tool arguments are inherently open-ended (every MCP tool defines
    # its own input schema) — `Any` values here are deliberate, not an
    # abuse of it; every other field on this model is concretely typed.
    arguments: dict[str, Any] = Field(default_factory=dict)

    reason: str

    hypothesis_id: HypothesisId | None = None
    expected_observation: str | None = None

    created_at: datetime
