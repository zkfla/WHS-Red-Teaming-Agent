"""Outbound-text gate for proposals: narrow first-storage reject.

Early stable API for root: evaluate_proposal_outbound and
evaluate_with_descriptor share one meaning with the Capability network
pre-check (same core narrow matcher). Inputs are the selected
ToolDescriptor outbound_text_fields declaration plus the engagement
ExternalTextPolicy prohibited_patterns. Outputs carry only a safe reason
code, never original argument text, values, or pattern contents.

Unknown descriptors follow the existing unavailable-tool diagnosis elsewhere
(no batch outbound reject here); declared descriptors narrow the check to
their listed fields, and a declared empty list means no external text is
sent so nothing is checked. Allowed original arguments are never redacted.
"""

from __future__ import annotations

from collections.abc import Sequence

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ExternalTextPolicy
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.outbound_text import OPERATOR_PATTERN_REASON, check_outbound_arguments

OUTBOUND_MATCHED_RULE = "outbound_text"


def _safe_reason(reason_code: str | None) -> list[str]:
    if reason_code is None:
        return []
    return ["outbound_text_blocked:" + reason_code]


def evaluate_proposal_outbound(
    proposal: ActionProposal,
    outbound_text_fields: Sequence[str],
    prohibited_patterns: Sequence[str],
) -> tuple[bool, list[str], str | None]:
    """Return (blocked, safe_reasons, reason_code) for declared fields."""
    verdict = check_outbound_arguments(
        proposal.arguments, outbound_text_fields, prohibited_patterns
    )
    if verdict.blocked:
        code = verdict.reason_code or OPERATOR_PATTERN_REASON
        return True, _safe_reason(code), code
    return False, [], None


def evaluate_with_descriptor(
    proposal: ActionProposal,
    descriptor: ToolDescriptor | None,
    external_policy: ExternalTextPolicy | None,
) -> tuple[bool, list[str], str | None]:
    """Proposal check against the actual descriptor declared fields only.

    A known descriptor (even with an empty outbound list) checks only its
    declared fields; empty means no external text so nothing is checked.
    An unknown binding (descriptor is None) returns pass here and follows
    the existing unavailable-tool diagnosis instead of batch-rejecting.
    """
    patterns: Sequence[str] = ()
    if external_policy is not None:
        patterns = external_policy.prohibited_patterns or ()
    if descriptor is None:
        return False, [], None
    fields: list[str] = [
        f for f in (descriptor.outbound_text_fields or []) if isinstance(f, str)
    ]
    return evaluate_proposal_outbound(proposal, fields, patterns)
