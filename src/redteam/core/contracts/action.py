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

from pydantic import BaseModel, Field, model_validator

from redteam.core.contracts.target import TargetRef
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    HypothesisId,
    RunId,
    ToolId,
)
from redteam.core.tool_binding import (
    LEGACY_ACTION,
    SELECTED_TOOL,
    BindingMode,
    execution_display_key,
)


class ActionProposal(BaseModel):
    action_id: ActionId
    run_id: RunId
    engagement_id: EngagementId
    agent_id: AgentId
    # A role may have several concurrent execution instances in one run.
    # None preserves proposals written before instance scheduling existed.
    instance_id: AgentInstanceId | None = None

    binding_mode: BindingMode = LEGACY_ACTION
    tool_id: ToolId | None = None
    capability: str = ""
    operation: str = ""

    target: TargetRef | None

    # Tool arguments are inherently open-ended (every MCP tool defines
    # its own input schema) — `Any` values here are deliberate, not an
    # abuse of it; every other field on this model is concretely typed.
    arguments: dict[str, Any] = Field(default_factory=dict)

    reason: str

    hypothesis_id: HypothesisId | None = None
    expected_observation: str | None = None

    # Agent-declared intent signal, not inferred from `operation` by
    # Control: does this specific action risk disrupting the target's
    # availability (flood/exhaustion/restart-style effects), as opposed to
    # a read-only or low-volume probe? Defaults to False ("no known
    # impact") so existing proposals are unaffected; a provider whose step
    # genuinely can affect availability must set this explicitly for
    # `RuleOfEngagement.availability_impact_allowed` to have any effect —
    # see `redteam.control.roe`.
    availability_impact: bool = False
    created_at: datetime

    @model_validator(mode="after")
    def _binding_is_consistent(self):
        if self.binding_mode == SELECTED_TOOL and not self.tool_id:
            raise ValueError("selected_tool binding requires tool_id")
        if self.target is None and self.binding_mode != SELECTED_TOOL:
            raise ValueError("legacy action requires a target")
        return self

    @property
    def action_key(self) -> str:
        return execution_display_key(self)
