"""LangGraph checkpointing.

One snapshot per super-step, enabling ``state_history`` and replay.

The allowlist is load-bearing: LangGraph only reconstructs types on an
explicit ``(module, name)`` allowlist and silently returns raw dicts for
anything else, so it is derived by walking our own packages rather than
typed out by hand. Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

#: Packages whose types may legitimately appear inside ``GraphState``.
STATE_PACKAGES = ("redteam.core", "redteam.attack_graph", "redteam.orchestration")


@lru_cache(maxsize=1)
def state_type_allowlist() -> tuple[tuple[str, str], ...]:
    """Every Pydantic model / Enum that can appear in ``GraphState``."""
    found: set[tuple[str, str]] = set()
    for package_name in STATE_PACKAGES:
        package = importlib.import_module(package_name)
        modules = [package]
        for info in pkgutil.walk_packages(
            getattr(package, "__path__", []), prefix=f"{package_name}."
        ):
            modules.append(importlib.import_module(info.name))

        for module in modules:
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if not obj.__module__.startswith("redteam."):
                    continue
                if issubclass(obj, (BaseModel, Enum)):
                    found.add((obj.__module__, obj.__name__))
    return tuple(sorted(found))


def build_checkpointer(*, enabled: bool = True) -> Any | None:
    """An in-memory saver, or ``None`` when checkpointing is off.

    TODO(agent-team): swap in a durable saver once runs must outlive the
    process; nothing outside this function changes.
    """
    if not enabled:
        return None

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return InMemorySaver(
        serde=JsonPlusSerializer(allowed_msgpack_modules=list(state_type_allowlist()))
    )
