"""``FakeControlPlane`` — test double for ``redteam.core.ports.control_plane.ControlPlane``.

NOT a production implementation — see ``tests/fakes/__init__.py``. The
real gate is ``redteam.control.service.DefaultControlPlane``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId


class FakeControlPlane:
    """Returns a fixed verdict for every proposal (default ALLOW), or a
    per-action override via ``verdict_by_action_id``. No scope/policy/
    rate-limit/budget logic — for tests of code that just needs *some*
    ``PolicyDecision`` back, not a correctly-evaluated one.
    """

    def __init__(
        self,
        default_verdict: PolicyVerdict = PolicyVerdict.ALLOW,
        verdict_by_action_id: dict[ActionId, PolicyVerdict] | None = None,
    ) -> None:
        self.default_verdict = default_verdict
        self.verdict_by_action_id = verdict_by_action_id or {}
        self.evaluated: list[ActionProposal] = []

    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision:
        self.evaluated.append(proposal)
        verdict = self.verdict_by_action_id.get(proposal.action_id, self.default_verdict)
        return PolicyDecision(
            action_id=proposal.action_id,
            verdict=verdict,
            reasons=["FakeControlPlane"],
            matched_rules=[],
            evaluated_at=datetime.now(timezone.utc),
        )
