"""One boundary for untrusted display text (redaction is not terminal escaping)."""

from __future__ import annotations

import re

from redteam.core.redaction import redact_sensitive_text

_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def safe_text(value: object, *, limit: int = 12000) -> str:
    text = redact_sensitive_text(str(value))
    text = _CONTROL.sub("", _ESCAPE.sub("", text))
    return text if len(text) <= limit else text[:limit] + "\n… 표시 길이 제한"
