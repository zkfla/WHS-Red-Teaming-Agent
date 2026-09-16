"""Audit log for Control Plane decisions and approval resolutions.

Two things this module records, both as an ``AuditEntry``:

- ``POLICY_DECISION`` -- every ``PolicyDecision`` the Control Plane
  produces (ALLOW/DENY/REQUIRE_APPROVAL), wired via
  ``DefaultControlPlane._finalize``/``evaluate``.
- ``APPROVAL_RESOLVED`` -- the human decision that eventually resolves a
  REQUIRE_APPROVAL verdict. This closes a real traceability gap: before
  this, a REQUIRE_APPROVAL verdict was recorded but nothing recorded
  whether a human later approved or rejected it. See
  ``DefaultControlPlane.resolve_approval``.

Not a ``core`` contract -- ``AuditEntry``/``AuditStore`` are entirely
Control Team's internal concern (nothing outside ``redteam.control``
reads them), so unlike ``EvidenceRecord``/``EvidenceStore`` this shape
can change freely without a Contract Change Proposal.

Backed by ``AuditStore`` (default: ``InMemoryAuditStore``, so existing
callers that build ``AuditLog()`` with no arguments keep working
unchanged). Pass ``redteam.control.stores.filesystem.FilesystemAuditStore``
for a durable sink.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, NewType, Protocol

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import Approval, PolicyDecision
from redteam.core.ids import ActionId, EngagementId, RunId, new_id

AuditId = NewType("AuditId", str)


class AuditEntryType(str, Enum):
    POLICY_DECISION = "policy_decision"
    APPROVAL_RESOLVED = "approval_resolved"


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|password|passwd|secret|credential|account|card|ssn|api[_-]?key|auth)",
    re.IGNORECASE,
)


def redact_arguments(value: Any) -> Any:
    """Best-effort redaction of likely-sensitive values before persisting.

    This is a safety net, not a substitute for not collecting sensitive
    data in the first place: it only catches values under an
    obviously-named key (token/password/account/card/...). Anything
    else -- including a real account number or session token sitting
    under an unrelated-looking key -- passes through unredacted. Recurses
    into nested dicts and lists so an ``ActionProposal.arguments`` blob
    of arbitrary shape is covered.
    """
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if _SENSITIVE_KEY_PATTERN.search(key) else redact_arguments(v))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [redact_arguments(item) for item in value]
    return value


class AuditEntry(BaseModel):
    """One append-only audit record -- either a policy decision or an
    approval resolution. Exactly one of ``decision``/``approval`` is set,
    matching ``entry_type``.
    """

    audit_id: AuditId
    entry_type: AuditEntryType
    run_id: RunId
    engagement_id: EngagementId
    action_id: ActionId

    # Redacted snapshot of the ActionProposal this entry concerns -- never
    # the raw proposal, so a token/account number in `arguments` never
    # reaches a durable sink verbatim.
    action_summary: dict[str, Any] | None = None

    decision: PolicyDecision | None = None
    approval: Approval | None = None

    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditStore(Protocol):
    """Durable sink for ``AuditEntry``. Mirrors ``EvidenceStore``'s shape
    deliberately (same append/list_for_run/list_for_action pattern) but
    is a plain internal Protocol, not a ``core`` port -- nothing outside
    ``redteam.control`` depends on it.
    """

    async def append(self, entry: AuditEntry) -> None: ...

    async def list_for_run(self, run_id: RunId) -> list[AuditEntry]: ...

    async def list_for_action(self, action_id: ActionId) -> list[AuditEntry]: ...


class InMemoryAuditStore:
    """Default backend -- process-local, lost on restart. Fine for tests
    and for any caller that doesn't need a durable audit trail yet.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> None:
        self._entries.append(entry)

    async def list_for_run(self, run_id: RunId) -> list[AuditEntry]:
        return [e for e in self._entries if e.run_id == run_id]

    async def list_for_action(self, action_id: ActionId) -> list[AuditEntry]:
        return [e for e in self._entries if e.action_id == action_id]


class AuditLog:
    """Control Team's audit service. ``DefaultControlPlane`` calls
    ``record`` after every ``PolicyDecision`` and
    ``record_approval_resolution`` from ``resolve_approval``.
    """

    def __init__(self, store: AuditStore | None = None) -> None:
        self._store = store or InMemoryAuditStore()

    async def record(self, action: ActionProposal, decision: PolicyDecision) -> None:
        entry = AuditEntry(
            audit_id=AuditId(new_id("audit")),
            entry_type=AuditEntryType.POLICY_DECISION,
            run_id=action.run_id,
            engagement_id=action.engagement_id,
            action_id=action.action_id,
            action_summary=redact_arguments(action.model_dump(mode="json")),
            decision=decision,
            created_at=datetime.now(timezone.utc),
        )
        await self._store.append(entry)

    async def record_approval_resolution(
        self, action: ActionProposal, approval: Approval
    ) -> None:
        entry = AuditEntry(
            audit_id=AuditId(new_id("audit")),
            entry_type=AuditEntryType.APPROVAL_RESOLVED,
            run_id=action.run_id,
            engagement_id=action.engagement_id,
            action_id=action.action_id,
            approval=approval,
            created_at=datetime.now(timezone.utc),
        )
        await self._store.append(entry)

    async def entries_for_run(self, run_id: RunId) -> list[AuditEntry]:
        return await self._store.list_for_run(run_id)

    async def entries_for_action(self, action_id: ActionId) -> list[AuditEntry]:
        return await self._store.list_for_action(action_id)
