from __future__ import annotations

import re

_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:secret|token|password|passwd|api[_-]?key|access[_-]?key)\b\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact(text: str) -> str:
    """Redact likely values while preserving GitHub secret expressions as references."""
    placeholders: list[str] = []

    def protect(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"__GH_SECRET_REF_{len(placeholders) - 1}__"

    protected = re.sub(r"\$\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*}}", protect, text)
    protected = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", protected)
    protected = _BEARER.sub(r"\1[REDACTED]", protected)
    protected = _ASSIGNMENT.sub(r"\1[REDACTED]", protected)
    for index, value in enumerate(placeholders):
        protected = protected.replace(f"__GH_SECRET_REF_{index}__", value)
    return protected
