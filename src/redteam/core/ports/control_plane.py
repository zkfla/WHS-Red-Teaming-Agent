"""Control Plane port — the single, mandatory gate before any Tool runs.

Orchestration calls only this Protocol. Agents must never reach the
Capability Layer (``redteam.core.ports.tool_gateway.ToolGateway``)
without an ALLOW ``PolicyDecision`` coming back from here first; that
rule is enforced structurally (Agent code has no import path to
``ToolGateway`` implementations) and checked by a dedicated test — see
``tests/unit/test_control_plane_bypass.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.ids import ApprovalId


class ControlPlane(Protocol):
    async def evaluate(
        self,
        proposal: ActionProposal,
        context: ControlContext,
    ) -> PolicyDecision: ...

    async def approval_outcome(self, approval_id: ApprovalId) -> PolicyDecision | None:
        """The final decision for a proposal that previously got
        ``REQUIRE_APPROVAL``, once a human has resolved it — or ``None``
        while still pending.

        Never creates a new Approval and never re-runs Scope/ROE/Policy
        (those already ran once, deterministically, against the same
        immutable proposal and engagement config): only the pipeline
        stages whose outcome can change between the original evaluation
        and resolution (rate limit, budget) are re-checked, so a resolved
        Approval cannot bypass either. A rejected or expired Approval
        resolves to a ``DENY`` here, never left dangling. Idempotent:
        calling this again after the first resolved call returns the
        same decision rather than re-consuming budget.
        """
        ...


@runtime_checkable
class InvocationAdmissionControl(Protocol):
    """Current checks and extra-call accounting, separate from initial approval.

    Existing narrow ControlPlane implementations remain usable. Production
    control implements this port; retries require its extra-call reservation.
    """

    async def admit_invocation(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
    ) -> PolicyDecision: ...

    async def reserve_extra_tool_call(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
        attempt_index: int,
    ) -> PolicyDecision: ...
