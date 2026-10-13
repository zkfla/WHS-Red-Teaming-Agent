"""Reasoning-effort vocabulary shared by config, CLI, web and providers.

The config-level vocabulary is a single owned contract. Provider/model wire
support is dynamic and verified at request time: a known level is forwarded
for the provider to accept or reject explicitly (never silently downgraded),
and an unknown string is rejected here.
"""

from __future__ import annotations

REASONING_EFFORTS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def normalize_reasoning_effort(value: object) -> str | None:
    """Canonical effort level, or ``None`` when unset.

    ``None``/blank keeps the provider/model default (historical behavior).
    An unknown non-blank string is a hard error so an explicit setting is
    never silently dropped; ``REDTEAM_LLM_REASONING_EFFORT`` is named in the
    message. No model-name allowlist lives here.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reasoning_effort must be a string or None")
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(
            f"unsupported reasoning effort {value!r}; "
            f"REDTEAM_LLM_REASONING_EFFORT must be one of: {allowed}"
        )
    return cleaned
