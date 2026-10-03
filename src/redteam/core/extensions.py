"""Extension declarations; collection is distinct from execution and permission.

Factories belong to their providing packages. The composition root connects
only selected declarations to the existing purpose-specific registries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pluggy

hookspec = pluggy.HookspecMarker("redteam")
hookimpl = pluggy.HookimplMarker("redteam")
ENTRY_POINT_GROUP = "redteam.extensions"


def value_factory(value: Any) -> Callable[[Any], Any]:
    """Provide already declared metadata; service factories still create per run."""

    def provide(_context: Any) -> Any:
        return value

    return provide


@dataclass(frozen=True)
class ContributionReference:
    kind: str
    contribution_id: str
    optional: bool = False


@dataclass(frozen=True)
class Contribution:
    """One registered implementation, with references checked before creation."""

    kind: str
    contribution_id: str
    version: str
    factory: Callable[[Any], Any]
    requires: tuple[ContributionReference, ...] = ()
    meaningful_change_keys: tuple[str, ...] = ()
    target_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionDeclaration:
    extension_id: str
    version: str
    contributions: tuple[Contribution, ...] = ()


class ExtensionHooks:
    @hookspec
    def redteam_extensions(self) -> tuple[ExtensionDeclaration, ...]:
        """Return declarations, without opening transports or invoking tools."""
        return ()
