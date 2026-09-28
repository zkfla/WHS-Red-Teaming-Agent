"""Deterministic redaction for the Operator Intent channel.

An operator instruction is free text: unlike a structured field it can
carry a raw secret ("use token=abc123"). The Intent channel must never
persist or publish raw secret material, so every instruction and every
parameter mapping is run through these helpers *before* it becomes an
``OperatorIntent`` (whose own validators only reject secret-shaped
*keys*, see ``core.contracts.mission``).

Redaction is deliberately conservative and deterministic — same input,
same output, no entropy heuristics:

- a secret-shaped key's value is replaced with ``***REDACTED***``
  (``*_ref`` / ``*_reference`` / ``*_id`` keys keep their opaque value);
- ``key=value`` / ``key: value`` spans whose key names a credential are
  replaced whole;
- ``Bearer <token>`` spans are replaced whole.

Secrets are never *stored* in redacted form for a structured parameter:
``secret_parameters`` drops them outright so a downstream validator can
never accidentally accept one.
"""

from __future__ import annotations

import re
from typing import Any

from redteam.core.redaction import redact_sensitive_text

REDACTED = "***REDACTED***"

#: Key names that always mean raw secret material (lower-cased, ``-``/``_``
#: normalized). Kept in sync with ``core.contracts.events._SECRET_KEYS``.
SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "api_key",
    "credential",
    "session",
    "authorization",
    "bearer",
    "private_key",
)

#: Suffixes that mark an *opaque reference* to a secret rather than the
#: secret itself (``credential_ref``, ``session_id``, ...).
_OPAQUE_SUFFIXES = ("_ref", "_reference", "_id")

_KEY_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|cookie|credential|"
    r"authorization|bearer|private[_-]?key|session[_-]?id)\b"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]+=*)")


def is_secret_key(key: str) -> bool:
    """True when ``key`` names raw secret material (not an opaque ref)."""
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized.endswith(_OPAQUE_SUFFIXES):
        return False
    return any(part in normalized for part in SECRET_KEY_PARTS)


def redact_text(text: str) -> str:
    """Return ``text`` with credential-shaped spans replaced."""
    if not text:
        return ""
    redacted = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    redacted = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
    return redact_sensitive_text(redacted)


def redact_mapping(value: Any) -> Any:
    """Recursively redact a JSON-shaped value.

    Secret-shaped keys become ``***REDACTED***``; opaque ``*_ref`` /
    ``*_id`` keys are preserved verbatim so a reference stays usable.
    Free-form strings are passed through ``redact_text``.
    """
    if isinstance(value, dict):
        return {
            str(key): (REDACTED if is_secret_key(str(key)) else redact_mapping(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def secret_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``parameters`` with secret-shaped keys removed entirely.

    ``OperatorIntent.parameters`` rejects secret keys, so a caller that
    supplies one by mistake gets a clean, secret-free mapping instead of
    a validation failure — and, crucially, nothing raw reaches storage.
    """
    cleaned: dict[str, Any] = {}
    for key, item in (parameters or {}).items():
        if is_secret_key(str(key)):
            continue
        cleaned[str(key)] = redact_mapping(item)
    return cleaned
