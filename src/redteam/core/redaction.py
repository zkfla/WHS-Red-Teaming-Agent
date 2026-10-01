"""Shared free-text redaction for contract-bound data."""

from __future__ import annotations

import re

#: Key names that mean raw secret/credential material -- the one shared
#: vocabulary for every *key-based* redactor in the codebase
#: (``core.contracts.events``, ``core.contracts.mission``,
#: ``orchestration.operator_intents.redaction``, ``app.cli._shared``).
#: Previously each of those independently declared its own copy of this
#: list, and they had silently drifted (some missing
#: ``proxy_authorization``/``set_cookie``/``session``/``private_key``
#: that others had). The *vocabulary* is shared here; each caller keeps
#: its own matching rule (whole-token vs. suffix vs. substring vs. regex)
#: and its own opaque-reference exemption -- redaction behavior is still
#: allowed to be context-specific, only the list of secret-shaped key
#: names is not.
SECRET_KEY_PARTS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "api_key",
        "credential",
        "session",
        "authorization",
        "proxy_authorization",
        "set_cookie",
        "bearer",
        "private_key",
    }
)

#: Suffixes marking an *opaque reference* to a secret, not the secret
#: itself (``credential_ref``, ``session_id``, ...) -- shared for the
#: same reason as ``SECRET_KEY_PARTS``.
OPAQUE_SUFFIXES = ("_ref", "_reference", "_id")

_SENSITIVE_TEXT_VALUE_PATTERN = re.compile(
    r"\b(api\s*[_-]?\s*key|access[_-]?token|refresh[_-]?token|session(?:[_-]?(?:id|token))?|"
    r"csrf(?:[_-]?token)?|token|cookie|password|passwd|secret|credential|account|card|ssn|"
    r"authorization|auth)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"\b(set-cookie|cookie|authorization)(\s*:\s*)[^\r\n]*",
    re.IGNORECASE,
)
_INLINE_CREDENTIAL_PATTERN = re.compile(
    r"\b(bearer\s+|(?:password|passwd|token|cookie|api\s*[_-]?\s*key|secret)"
    r"\s+(?:is\s+)?)([^\s,;]+)",
    re.IGNORECASE,
)


def redact_sensitive_text(value: str) -> str:
    """Redact credential headers and common key/value secrets in free text."""
    value = _SENSITIVE_HEADER_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***",
        value,
    )
    value = _SENSITIVE_TEXT_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***",
        value,
    )
    return _INLINE_CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}***REDACTED***", value
    )
