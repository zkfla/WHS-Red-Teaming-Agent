"""Control Plane port — the single, mandatory gate before any Tool runs.

Orchestration calls only this Protocol. Agents must never reach the
Capability Layer (``redteam.core.ports.tool_gateway.ToolGateway``)
without an ALLOW ``PolicyDecision`` coming back from here first; that
rule is enforced structurally (Agent code has no import path to
``ToolGateway`` implementations) and checked by a dedicated test — see
``tests/unit/test_control_plane_bypass.py``.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext, PolicyDecision


class ControlPlane(Protocol):
    async def evaluate(
        self,
        proposal: ActionProposal,
        context: ControlContext,
    ) -> PolicyDecision: ...
