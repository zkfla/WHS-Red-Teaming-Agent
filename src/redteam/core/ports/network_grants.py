"""Control-owned capacity authority consumed by trusted gate controllers.

Model inputs, stdout, and evidence copies cannot authorize a grant. The
composition boundary binds one ledger to one engagement and the controller
binds requests to its own invocation/instance and launched daemon handle.
Kernel inspection, scope resolution and wire protocol are provider concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redteam.core.contracts.network import (
    AdmissionDecision,
    GrantRefused,
    MeterReading,
    NetworkGrant,
    WindowCaps,
    WindowSettlement,
    WindowState,
)


class NetworkGrantLedgerReadError(Exception):
    """Stable, machine-readable failure from a read-only grant inventory.

    A failed or corrupt read is never represented as an empty inventory.
    ``reason_code`` is deliberately independent of backend exception text.
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class OutstandingNetworkWindow:
    """One exact original grant still OPEN or QUARANTINED in Control."""

    grant: NetworkGrant
    state: WindowState
    settlement: WindowSettlement | None


class NetworkGrantLedger(Protocol):
    async def request_window(
        self, *, run_id: str, instance_id: str, epoch: int, caps: WindowCaps
    ) -> NetworkGrant | GrantRefused:
        """Atomically reserve complete byte/delivery caps from both purses.

        An open or quarantined instance window refuses a new window.
        Deadlines never free capacity or ownership by themselves.
        """
        ...

    async def admit_delivery(
        self, *, window_id: str, daemon_session_id: str, seq: int, flow_key: str
    ) -> AdmissionDecision:
        """Commit before returning ACCEPT; duplicates must identify the SAME
        currently held packet from the SAME trusted daemon session. New
        packets cannot reuse sequences, including after daemon restart.
        """
        ...

    async def settle_window(
        self,
        *,
        window_id: str,
        meter: MeterReading | None,
        verified_closed: bool,
    ) -> WindowSettlement:
        """Return proven-unused capacity once after verified closed boundary
        AND descendant teardown. Unknown/malformed/over-cap meters retain
        complete reservations and quarantine the window. Replays return
        the stored settlement without crediting capacity again.
        """
        ...

    async def window_grant(self, *, window_id: str) -> NetworkGrant | None:
        """Original grant for one window id, or None when unknown.

        Read-only identity lookup at the connection boundary: recovery paths
        verify a retained record against this original (run, instance, epoch,
        purpose) before settling. Never authorizes transmission.
        """
        ...

    async def outstanding_windows(
        self, *, run_id: str, instance_id: str
    ) -> tuple[OutstandingNetworkWindow, ...]:
        """Read exact-run/instance OPEN and QUARANTINED grants.

        The ledger is already bound to one engagement. An empty tuple means a
        successful query found no outstanding rows; invalid scope, storage
        failure, or corrupt row data raises ``NetworkGrantLedgerReadError``.
        This query grants no transmission or settlement authority.
        """
        ...

    async def release_quarantine(self, *, window_id: str, verified_closed: bool) -> bool:
        """Allow a fresh epoch only after authoritative closed/teardown
        reconciliation. Previously retained charges are never refunded.
        """
        ...
