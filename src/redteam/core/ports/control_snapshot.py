"""Owner inspection for an explicit offline import, never live admission."""

from sqlite3 import Connection
from typing import Protocol

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import Approval


class ControlSnapshotInspection(Protocol):
    async def inspect_execution_approval(self, proposal: ActionProposal) -> Approval | None: ...

    async def get_approval(self, approval_id: str) -> Approval | None: ...

    def check_network(self, rows: dict[str, list[dict]]) -> None: ...

    def import_network(self, connection: Connection, rows: dict[str, list[dict]]) -> None:
        """Borrow the caller's offline transaction; never reserve or refund quota."""
        ...
