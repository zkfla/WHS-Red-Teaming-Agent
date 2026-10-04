"""Narrow outbound-text guard: explicit credentials plus operator patterns.

Canonical source for the shared narrow check. Both Control (first
judgement, admit, extra-call checks) and Capability network pre-checks
(web search, page reader) consume these public functions so every layer
uses the same meaning. Do not import private matchers from capability
modules and do not duplicate these expressions elsewhere.

Scope: narrow by design. Explicit credentials means an assignment-shaped
credential (key followed by colon or equals) or a bearer token value.
A bare word such as token inside normal prose does not block. Storage
masking (redact_sensitive_text) is broader and stays separate; its output
must never be reused as a block predicate.

Operator prohibited_patterns are literal case-insensitive substrings
(empty or blank entries are ignored). They are matched literally, never
as regular expressions, to avoid ReDoS and surprise matches. Reason codes
never carry the original text, field values, or pattern contents.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EXPLICIT_CREDENTIAL_REASON = "explicit_credential"
OPERATOR_PATTERN_REASON = "operator_prohibited_pattern"

_EXPLICIT_ASSIGNMENT = re.compile(
    r"\b(?:authorization|cookie|password|passwd|token|api[_-]?key|apikey|"
    r"access[_-]?token|refresh[_-]?token|secret)\s*[:=]",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)


@dataclass(frozen=True)
class OutboundTextVerdict:
    """Result of a narrow outbound-text check (no original text stored)."""

    blocked: bool
    reason_code: str | None


def contains_explicit_credential(text: str) -> bool:
    """True when text carries an explicit credential shape."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_EXPLICIT_ASSIGNMENT.search(text) or _BEARER_VALUE.search(text))


def matches_prohibited_patterns(text: str, prohibited_patterns: Sequence[str]) -> bool:
    """True when any operator pattern occurs in text (literal, case-insensitive)."""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    for pattern in prohibited_patterns:
        if not isinstance(pattern, str):
            continue
        needle = pattern.strip().lower()
        if not needle:
            continue
        if needle in lowered:
            return True
    return False


def check_outbound_text(
    value: str,
    prohibited_patterns: Sequence[str] = (),
) -> OutboundTextVerdict:
    """Check one outbound string value; explicit credentials first.

    Returns blocked plus a safe reason code (no original text).
    """
    if contains_explicit_credential(value):
        return OutboundTextVerdict(blocked=True, reason_code=EXPLICIT_CREDENTIAL_REASON)
    if matches_prohibited_patterns(value, prohibited_patterns):
        return OutboundTextVerdict(blocked=True, reason_code=OPERATOR_PATTERN_REASON)
    return OutboundTextVerdict(blocked=False, reason_code=None)


def check_outbound_arguments(
    arguments: Mapping[str, Any],
    outbound_text_fields: Sequence[str],
    prohibited_patterns: Sequence[str] = (),
) -> OutboundTextVerdict:
    """Check declared outbound fields of a tool invocation.

    Only the fields named in outbound_text_fields (the selected
    ToolDescriptor declaration) are inspected. String list or tuple values
    are checked element-wise; non-string scalars are ignored. Empty field
    lists check nothing here; callers that need a fail-closed fallback for
    unknown descriptors handle that in control.outbound instead.
    """
    for field_name in outbound_text_fields:
        if not isinstance(field_name, str):
            continue
        if field_name not in arguments:
            continue
        value = arguments[field_name]
        candidates: list[str] = []
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple)):
            candidates = [item for item in value if isinstance(item, str)]
        else:
            continue
        for candidate in candidates:
            verdict = check_outbound_text(candidate, prohibited_patterns)
            if verdict.blocked:
                return verdict
    return OutboundTextVerdict(blocked=False, reason_code=None)
