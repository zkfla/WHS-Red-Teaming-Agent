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

from datetime import datetime, timezone
from enum import Enum
from typing import Any, NewType, Protocol

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import Approval, PolicyDecision
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, ApprovalId, EngagementId, RunId, new_id
from redteam.core.redaction import redact_arguments as redact_arguments
from redteam.core.redaction import redact_sensitive_text

AuditId = NewType("AuditId", str)


class AuditEntryType(str, Enum):
    POLICY_DECISION = "policy_decision"
    APPROVAL_RESOLVED = "approval_resolved"


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
            decision=decision.model_copy(
                update={
                    "reasons": [redact_sensitive_text(reason) for reason in decision.reasons],
                    "matched_rules": [
                        redact_sensitive_text(rule) for rule in decision.matched_rules
                    ],
                }
            ),
            created_at=datetime.now(timezone.utc),
        )
        await self._store.append(entry)

    async def record_approval_resolution(
        self, action: ActionProposal, approval: Approval
    ) -> None:
        await self._append_resolution(
            action.run_id, action.engagement_id, action.action_id, approval
        )

    async def find_approval_request(
        self, action_id: ActionId, approval_id: ApprovalId
    ) -> AuditEntry | None:
        """Recover the original request context from durable policy audit."""
        for entry in await self._store.list_for_action(action_id):
            if (
                entry.entry_type == AuditEntryType.POLICY_DECISION
                and entry.decision is not None
                and entry.decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
                and entry.decision.approval_id == approval_id
            ):
                return entry
        return None

    async def record_approval_resolution_from_request(
        self, request: AuditEntry, approval: Approval
    ) -> None:
        """Record a resolution using context recovered after a process restart."""
        if request.action_id != approval.action_id:
            raise ValueError("approval action_id does not match audit request")
        if (
            request.entry_type != AuditEntryType.POLICY_DECISION
            or request.decision is None
            or request.decision.verdict != PolicyVerdict.REQUIRE_APPROVAL
            or request.decision.approval_id != approval.approval_id
        ):
            raise ValueError("approval_id does not match audit request")
        await self._append_resolution(
            request.run_id, request.engagement_id, request.action_id, approval
        )

    async def _append_resolution(
        self, run_id: RunId, engagement_id: EngagementId, action_id: ActionId, approval: Approval
    ) -> None:
        entry = AuditEntry(
            audit_id=AuditId(new_id("audit")),
            entry_type=AuditEntryType.APPROVAL_RESOLVED,
            run_id=run_id,
            engagement_id=engagement_id,
            action_id=action_id,
            approval=approval,
            created_at=datetime.now(timezone.utc),
        )
        await self._store.append(entry)

    async def entries_for_run(self, run_id: RunId) -> list[AuditEntry]:
        return await self._store.list_for_run(run_id)

    async def entries_for_action(self, action_id: ActionId) -> list[AuditEntry]:
        return await self._store.list_for_action(action_id)
