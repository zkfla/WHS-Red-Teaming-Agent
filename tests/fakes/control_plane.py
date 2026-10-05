"""``FakeControlPlane`` — test double for ``redteam.core.ports.control_plane.ControlPlane``.

NOT a production implementation — see ``tests/fakes/__init__.py``. The
real gate is ``redteam.control.service.DefaultControlPlane``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, ApprovalId


class FakeControlPlane:
    """Returns a fixed verdict for every proposal (default ALLOW), or a
    per-action override via ``verdict_by_action_id``. No scope/policy/
    rate-limit/budget logic — for tests of code that just needs *some*
    ``PolicyDecision`` back, not a correctly-evaluated one.

    ``approval_outcome`` returns a canned decision set via
    ``resolve_approval`` (test setup), or ``None`` (still pending) if
    never set — real budget/rate-limit re-evaluation belongs to
    ``DefaultControlPlane``, not this fake.
    """

    def __init__(
        self,
        default_verdict: PolicyVerdict = PolicyVerdict.ALLOW,
        verdict_by_action_id: dict[ActionId, PolicyVerdict] | None = None,
    ) -> None:
        self.default_verdict = default_verdict
        self.verdict_by_action_id = verdict_by_action_id or {}
        self.evaluated: list[ActionProposal] = []
        self._resolved: dict[ApprovalId, PolicyDecision] = {}

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        self.evaluated.append(proposal)
        verdict = self.verdict_by_action_id.get(proposal.action_id, self.default_verdict)
        return PolicyDecision(
            action_id=proposal.action_id,
            verdict=verdict,
            reasons=["FakeControlPlane"],
            matched_rules=[],
            approval_id=ApprovalId(f"appr_{proposal.action_id}")
            if verdict == PolicyVerdict.REQUIRE_APPROVAL
            else None,
            evaluated_at=datetime.now(timezone.utc),
        )

    def resolve_approval(self, approval_id: ApprovalId, decision: PolicyDecision) -> None:
        """Test setup helper: make ``approval_outcome`` return ``decision``."""
        self._resolved[approval_id] = decision

    async def approval_outcome(
        self, approval_id: ApprovalId, current_proposal: ActionProposal | None = None
    ) -> PolicyDecision | None:
        # Port-signature parity only: the extra proposal is accepted so this
        # double stays assignable to the public port, but it never changes
        # the canned outcome. Re-judgement (stale/mismatch DENY) belongs to
        # the real gate, not this fake.
        _ = current_proposal
        return self._resolved.get(approval_id)

    async def admit_invocation(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
    ) -> PolicyDecision:
        """The fake has no accounting; preserve its configured current verdict."""
        verdict = self.verdict_by_action_id.get(proposal.action_id, self.default_verdict)
        if verdict is PolicyVerdict.REQUIRE_APPROVAL:
            verdict = decision.verdict
        return decision.model_copy(update={"verdict": verdict})

    async def reserve_extra_tool_call(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
        attempt_index: int,
    ) -> PolicyDecision:
        return await self.admit_invocation(proposal, decision, context, receipt_id=receipt_id)
