"""Shared Attack Graph. Owned by Agent Team.

``stores/`` holds ``AttackGraphStore`` implementations
(``redteam.core.ports.attack_graph``); ``service.py`` is the
higher-level API orchestration/agents use so they don't touch a store
directly.
"""

from redteam.attack_graph.service import AttackGraphService

__all__ = ["AttackGraphService"]
