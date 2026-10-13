"""Reasoning-effort wire mapping for the LLM adapters.

Core owns the vocabulary; this module maps a level onto each protocol:
top-level ``reasoning_effort`` everywhere, plus native ``output_config.effort``
on Messages (SDK + provider docs). Model support is dynamic and explicit;
an explicit effort refuses any enabled ``drop_params``. Opaque thinking
blocks are never rewritten here; their capture/replay is owned elsewhere.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from redteam.core.contracts.llm_config import (
    REASONING_EFFORTS as REASONING_EFFORTS,
)
from redteam.core.contracts.llm_config import (
    normalize_reasoning_effort as normalize_reasoning_effort,
)

__all__ = [
    "REASONING_EFFORTS",
    "normalize_reasoning_effort",
    "OUTPUT_CONFIG_EFFORTS",
    "litellm_default_kwargs",
    "apply_messages_effort",
    "litellm_connection_fingerprint_for_config",
    "llm_connection_fingerprint",
]

_LITELLM_PROVIDER = "litellm"

# Verified native ``output_config.effort`` values: mirrors the installed
# SDK map (REASONING_EFFORT_TO_OUTPUT_CONFIG_EFFORT) and the provider docs.
# ``minimal`` rides as ``low``; ``none``/unset sets no key (the provider
# rejects any other value loudly at request time).
OUTPUT_CONFIG_EFFORTS: dict[str, str] = {
    "low": "low",
    "minimal": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def litellm_default_kwargs(effort: str | None) -> dict[str, Any]:
    """Request defaults carrying an explicit effort, or ``{}`` when unset.

    An unset effort returns no keys so historical request bytes are
    unchanged. A set effort forwards top-level ``reasoning_effort`` on
    every protocol (chat, responses, messages); unknown values raise
    loudly via the core normalizer. ``drop_params`` is deliberately NOT
    set here (its Responses-path support is unverified); the silent-drop
    guard lives in ``LiteLLMClient``, which refuses an enabled
    ``drop_params`` whenever an explicit effort is present.
    """
    level = normalize_reasoning_effort(effort)
    if level is None:
        return {}
    return {"reasoning_effort": level}


def apply_messages_effort(
    output_config: Mapping[str, Any] | None, effort: str | None
) -> dict[str, Any]:
    """Merge native effort into ``output_config`` without losing fields.

    Returns a copy of any existing mapping (caller fields and ``format``
    survive), plus native ``effort`` for an explicit level per the
    verified provider contract. ``None``/blank/``none`` sets no ``effort``
    key; unknown levels raise loudly via the core normalizer.
    """
    level = normalize_reasoning_effort(effort)
    merged = dict(output_config) if isinstance(output_config, Mapping) else {}
    if level is None or level == "none":
        return merged
    merged["effort"] = OUTPUT_CONFIG_EFFORTS[level]
    return merged


def litellm_connection_fingerprint_for_config(llm_config: Any) -> str | None:
    """Secret-free identity for explicit LiteLLM effort; None when unset.

    ``None`` for any non-``litellm`` provider and for unset effort, so the
    historical unpinned identity is byte-identical. An explicit effort
    yields a stable hash over provider, model, credential REFERENCE (never
    the value), and effort -- a changed effort alters the binding and
    refuses resume, while key-value rotation under the same reference
    still rebuilds.
    """
    provider = getattr(llm_config, "provider", None) or ""
    if not isinstance(provider, str) or provider.strip() != _LITELLM_PROVIDER:
        return None
    effort = normalize_reasoning_effort(getattr(llm_config, "reasoning_effort", None))
    if effort is None:
        return None
    model = getattr(llm_config, "default_model", "") or ""
    credential = getattr(llm_config, "api_key_env_var", "") or ""
    canonical = json.dumps(
        {
            "v": "v1",
            "provider": _LITELLM_PROVIDER,
            "model": model.strip(),
            "credential": credential,
            "reasoning_effort": effort,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def llm_connection_fingerprint(llm_config: Any) -> str | None:
    """Shared resume preflight: explicit effort alters binding per provider.

    ``litellm`` with explicit effort gets the LiteLLM identity (``None``
    when unset); ``opencode-go`` keeps its own helper (extended with
    effort); anything else stays ``None``.
    """
    provider = getattr(llm_config, "provider", None) or ""
    if isinstance(provider, str) and provider.strip() == _LITELLM_PROVIDER:
        return litellm_connection_fingerprint_for_config(llm_config)
    # Local import: opencode_go_config stays import-light for bootstrap/CLI,
    # and each canonical lives in exactly one place.
    from redteam.llm.opencode_go_config import (
        llm_connection_fingerprint_for_config as _go_fingerprint,
    )

    return _go_fingerprint(llm_config)
