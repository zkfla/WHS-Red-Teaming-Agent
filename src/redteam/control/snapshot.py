"""Control-owned original approval inspection and network snapshot participant."""

from sqlite3 import Connection

from redteam.control.approval import ApprovalStore
from redteam.control.audit import AuditStore, inspect_execution_approval
from redteam.control.network_ledger import check_network_snapshot, import_network_snapshot
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import Approval
from redteam.core.ids import ApprovalId


class ControlSnapshotInspector:
    def __init__(self, audit: AuditStore | None, approvals: ApprovalStore | None):
        self.audit, self.approvals = audit, approvals

    async def inspect_execution_approval(self, proposal: ActionProposal) -> Approval | None:
        if self.audit is None:
            raise ValueError("original_control_audit_missing")
        return await inspect_execution_approval(self.audit, self.approvals, proposal)

    async def get_approval(self, approval_id: str) -> Approval | None:
        if self.approvals is None:
            raise ValueError("migration_original_approval_store_missing")
        return await self.approvals.get(ApprovalId(approval_id))

    def check_network(self, rows: dict[str, list[dict]]) -> None:
        check_network_snapshot(rows)

    def import_network(self, connection: Connection, rows: dict[str, list[dict]]) -> None:
        import_network_snapshot(connection, rows)
