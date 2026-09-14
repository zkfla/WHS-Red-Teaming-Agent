"""Audit log for Control Plane decisions.

In-memory list for now; TODO(control-team): durable sink (file/db) and
structured logging integration.
"""

from __future__ import annotations

from dataclasses import dataclass

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision


@dataclass
class AuditEntry:
    action: ActionProposal
    decision: PolicyDecision


class AuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, action: ActionProposal, decision: PolicyDecision) -> None:
        self._entries.append(AuditEntry(action=action, decision=decision))

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)
