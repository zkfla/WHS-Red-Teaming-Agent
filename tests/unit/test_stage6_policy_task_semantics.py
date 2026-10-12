"""Stage 6 policy-denial and Task semantics (Prompt 6).

A Control ``DENY`` is a *decision*, never a tool failure. Orchestration must:

* record the block without touching ``consecutive_failures`` / retry counters
  (EXECUTE owns those; a block never reached EXECUTE);
* react to Control's severity hint, carried in
  ``PolicyDecision.matched_rules`` as ``"severity:<level>"`` because the frozen
  contract has no dedicated field:
    - ``BLOCK_ACTION``  -> block only this action; replan toward the same goal;
    - ``REPLAN_GOAL``   -> LLM replans (task stays RUNNING + feedback);
      no automatic task blocking or goal switching in code;
    - ``ABORT_RUN``     -> nothing more can proceed; end the run with a named
      reason;
* survive a denial whose active Task is already settled -- a REJECTED approval
  can arrive after the work was closed elsewhere and must replan, not crash.
* policy admission needs an active path; tests register a matching active
  PathPlan/PathStep so the real policy path runs (no legacy fallback).
* denial evidence is the work ledger (BLOCKED) and bus events
  (ACTION_DENIED/GRAPH_UPDATED), not graph facts.

The fan-out severity hand-off is exercised separately in
``test_stage6_fanout_policy_semantics``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.severity import Severity, to_tag
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.enums import (
    PolicyVerdict,
)
from redteam.core.ids import (
    ActionId,
    EngagementId,
    MissionId,
    RunId,
)
from redteam.orchestration.nodes.policy import denial_severity

NOW = datetime.now(timezone.utc)
RUN = RunId("run_stage6_policy")
ENG = EngagementId("eng_stage6_policy")
MISSION = MissionId("mission_stage6_policy")






def decision(
    verdict: PolicyVerdict = PolicyVerdict.DENY,
    severity: Severity | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        action_id=ActionId("action_policy"),
        verdict=verdict,
        reasons=["stage6"],
        matched_rules=[to_tag(severity)] if severity is not None else [],
        evaluated_at=NOW,
    )














# -- severity tag reader -------------------------------------------------


def test_denial_severity_reads_every_control_tag() -> None:
    for severity in Severity:
        tagged = decision(PolicyVerdict.DENY, severity)
        assert denial_severity(tagged) == severity.value


def test_denial_severity_is_none_when_untagged_or_malformed() -> None:
    assert denial_severity(None) is None
    assert denial_severity(decision(PolicyVerdict.DENY, None)) is None
    assert denial_severity(decision(PolicyVerdict.ALLOW, Severity.ABORT_RUN)) is None
    malformed = PolicyDecision(
        action_id=ActionId("a"),
        verdict=PolicyVerdict.DENY,
        matched_rules=["severity:not_a_level"],
        evaluated_at=NOW,
    )
    # Unknown level strings are surfaced verbatim; only the *matching* levels
    # in REPLAN cause a reaction, so an unknown one degrades to "replan".
    assert denial_severity(malformed) == "not_a_level"


# -- denial is a block, not a tool failure -------------------------------




# -- severity-driven reaction in REPLAN ----------------------------------










# -- the approval-deny resume actually replans ---------------------------
