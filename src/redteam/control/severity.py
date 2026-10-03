"""Typed denial severity plus the original tag compatibility API."""

from __future__ import annotations

from redteam.core.contracts.policy import legacy_denial_severity

# The one Severity definition (core, not here -- see its docstring for why):
# re-exported under this name so every existing `from
# redteam.control.severity import Severity` caller is unaffected.
from redteam.core.enums import Severity

__all__ = ["Severity", "to_tag", "severity_from_matched_rules"]

_TAG_PREFIX = "severity:"


def to_tag(severity: Severity) -> str:
    """The ``matched_rules`` token that carries ``severity``."""
    return f"{_TAG_PREFIX}{severity.value}"


def severity_from_matched_rules(matched_rules: list[str]) -> Severity | None:
    """Recover the severity a decision carries, or ``None`` if untagged."""
    value = legacy_denial_severity(matched_rules)
    try:
        return Severity(value) if value is not None else None
    except ValueError:
        return None
