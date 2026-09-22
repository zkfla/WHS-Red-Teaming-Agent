"""Denial severity — a Control-side hint about how the orchestrator
should react to a non-ALLOW decision.

Control decides *whether* an action passes and *why*; the orchestrator
(Agent Team) decides what the run does next. This module lets Control
attach a machine-readable recommendation to that decision so the two
stay decoupled.

Transport: since ``PolicyDecision`` (a ``core`` contract, frozen at
v0.1) has no ``severity`` field, the tag is carried inside
``PolicyDecision.matched_rules`` as ``"severity:<value>"``. Read it back
with ``severity_from_matched_rules``. Promoting this to a first-class
``PolicyDecision.severity`` field is a candidate PM/contract change; the
tag convention is the no-change starting point (see docs/control/PLAN.md
§3).

Levels (what the orchestrator is expected to do — enforcement is Agent
Team's, not done here):

  - ``BLOCK_ACTION`` — block only this action; retry a *different action*
    toward the same goal. (e.g. one target out of scope, one operation
    denied, rate limit hit.)
  - ``REPLAN_GOAL`` — this goal/capability is not viable at all; switch
    to a *different goal*. (e.g. a whole capability is ROE-forbidden.)
  - ``ABORT_RUN`` — nothing more can proceed; end the run. (e.g. outside
    the allowed time window, execution budget exhausted.)
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    BLOCK_ACTION = "block_action"
    REPLAN_GOAL = "replan_goal"
    ABORT_RUN = "abort_run"


_TAG_PREFIX = "severity:"


def to_tag(severity: Severity) -> str:
    """The ``matched_rules`` token that carries ``severity``."""
    return f"{_TAG_PREFIX}{severity.value}"


def severity_from_matched_rules(matched_rules: list[str]) -> Severity | None:
    """Recover the severity a decision carries, or ``None`` if untagged."""
    for rule in matched_rules:
        if rule.startswith(_TAG_PREFIX):
            try:
                return Severity(rule[len(_TAG_PREFIX) :])
            except ValueError:
                return None
    return None
