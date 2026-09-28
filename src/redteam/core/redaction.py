"""Shared free-text redaction for contract-bound data."""

from __future__ import annotations

import re

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
