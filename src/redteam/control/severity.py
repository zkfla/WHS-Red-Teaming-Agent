"""Denial severity — a Control-side hint about how the orchestrator
should react to a non-ALLOW decision.

Control decides *whether* an action passes and *why*; the orchestrator
(Agent Team) decides what the run does next. This module lets Control
attach a machine-readable recommendation to that decision so the two
stay decoupled. ``Severity`` itself, and what each level means, is
defined once in ``redteam.core.enums`` (re-exported here) -- see that
docstring, not this one, for the three levels.

Transport: since ``PolicyDecision`` (a ``core`` contract, frozen at
v0.1) has no ``severity`` field, the tag is carried inside
``PolicyDecision.matched_rules`` as ``"severity:<value>"``. Read it back
with ``severity_from_matched_rules``. Promoting this to a first-class
``PolicyDecision.severity`` field is a candidate PM/contract change --
evaluated during the architecture-consolidation pass this module's
docstring used to point at (docs/control/PLAN.md §3) and deliberately
deferred: ``PolicyDecision.matched_rules`` carrying arbitrary/malformed
severity strings through untouched (rather than a validating enum field
rejecting them) is itself real, tested behavior
(``tests/unit/test_stage6_policy_task_semantics.py``'s
``test_denial_severity_is_none_when_untagged_or_malformed``), not
slack to clean up incidentally. The tag convention remains the
no-change transport; only the ``Severity`` *definition* itself is now
single-sourced (previously independently declared here and, as three
bare string constants, in ``orchestration.nodes.policy``).
"""

from __future__ import annotations

# The one Severity definition (core, not here -- see its docstring for why):
# re-exported under this name so every existing `from
# redteam.control.severity import Severity` caller is unaffected.
from redteam.core.enums import Severity

__all__ = ["Severity", "to_tag", "severity_from_matched_rules"]

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
