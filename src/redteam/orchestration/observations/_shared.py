"""Pure helpers shared by tool-specific Result Mappers.

Each mapper (``recon_http``, ``recon_inventory``, ``recon_network``,
``web_http``, ``web_proxy``) owns its own tool-specific interpretation of
a ``ToolResult`` — that stays separate on purpose (docs/agent/orchestration-
implementation-plan.md section 6). What was duplicated verbatim across
several of them (a hex-digest shape check, a path-traversal-safe string
check, a deterministic content-hash key) is a handful of pure, tool-
agnostic functions, not mapper logic — sharing them here removes the
duplication without merging the mappers themselves or introducing a new
class-based abstraction (no ``ValidationService``/``Factory``/``Registry``).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, TypeGuard

#: A lowercase or mixed-case SHA-256 hex digest, and nothing else --
#: shared so every mapper that validates a tool-supplied digest (before
#: trusting it as a stable identity/comparison key) applies the exact
#: same shape check.
HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")


def is_hex64(value: object) -> TypeGuard[str]:
    """Whether ``value`` is a well-formed 64-character hex digest.

    ``TypeGuard[str]`` so ``if not is_hex64(value): return ...`` narrows
    ``value`` to ``str`` afterward -- the same narrowing each call site's
    old inline ``isinstance(value, str) and _HEX_64.fullmatch(value)``
    gave mypy directly, now preserved through this shared function.
    """
    return isinstance(value, str) and HEX_64.fullmatch(value) is not None


def safe_path(value: Any) -> str | None:
    """A tool-reported path, or ``None`` if it is not safe to record/reuse.

    Rejects anything that is not an absolute, traversal-free, control-
    character-free path a follow-up request could reuse verbatim: this
    is a defensive shape check on data a tool observed, not a statement
    that the path is authorized (Control still decides that).
    """
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
        return None
    if any(ord(char) < 32 or char in "?#%\\" for char in value):
        return None
    if "//" in value or any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def deterministic_key(kind: str, *parts: object) -> str:
    """A stable, content-sensitive identity for one observation context.

    Same inputs -> same key, across processes and runs: used as a
    ``context_key`` so a later turn can recognize "this is the same
    comparison/observation as before" without re-deriving it from a full
    object. Not a security digest -- callers needing a validated,
    tool-supplied hash use :func:`is_hex64` instead.
    """
    encoded = json.dumps([kind, *parts], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
