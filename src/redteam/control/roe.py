"""ROE (Rules of Engagement) stage — runs after Scope, before Policy, in
``DefaultControlPlane.evaluate`` (instruction.md section 9 pipeline).

SKELETON (control-team, track-a): this is a pass-through that allows
everything, so the pipeline shape is complete and wiring is testable
without changing any current behavior. Fill in real enforcement against
``Engagement.rules_of_engagement`` here.

Return ``(False, [reasons])`` to DENY the action, ``(True, [])`` to let
it continue down the pipeline.

TODO(control-team, track-a): evaluate each ``RuleOfEngagement`` against
the proposal —
  - allowed/denied capabilities   (``allowed_capabilities`` / ``denied_capabilities``)
  - allowed/denied operations     (``allowed_operations`` / ``denied_operations``)
  - availability impact           (``availability_impact_allowed``)
  - time windows                  (``start_at`` / ``end_at`` / ``allowed_time_windows``)
  - prohibited techniques         (``prohibited_techniques``)
"""

from __future__ import annotations

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement


def check_roe(action: ActionProposal, engagement: Engagement) -> tuple[bool, list[str]]:
    """Evaluate the proposal against the engagement's Rules of Engagement.

    SKELETON: pass-through — allows everything for now. See the module
    docstring TODO(control-team, track-a) for the rules to implement.
    ``action`` and ``engagement`` are already in the signature so the
    real implementation needs no call-site change in ``service.py``.
    """
    del action, engagement  # unused until real ROE rules are implemented
    return True, []
