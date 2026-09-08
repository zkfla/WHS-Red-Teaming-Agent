from __future__ import annotations

import re

_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b"
    r"(\s*[:=]\s*)([^\s,;}{]{4,})"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_free_text(value: str) -> str:
    """Best-effort deterministic redaction for non-authoritative model-written text.

    This is a storage/logging safety layer, not a credential detector and not security evidence.
    """

    cleaned = "".join(character for character in value if character >= " " or character in "\n\t")
    cleaned = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", cleaned)
    cleaned = _BEARER.sub("Bearer [REDACTED]", cleaned)
    cleaned = _JWT.sub("[REDACTED_JWT]", cleaned)
    cleaned = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", cleaned)
    return cleaned.strip()
