"""Narrow host recovery port for Control-owned gated network windows.

Capability reports observations and per-window Control settlement outcomes;
the host Run recovery path retains ownership, checkpoint, and actor-admission
authority. This port never resumes an actor or authorizes a new transmission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redteam.core.contracts.network import WindowSettlement


class PreparedDockerEndpoint(Protocol):
    """Structural view of the endpoint restored by shell preparation."""

    @property
    def binary(self) -> str: ...

    @property
    def context(self) -> str: ...

    @property
    def host(self) -> str: ...


@dataclass(frozen=True)
class GatedWindowClosureResult:
    """Read-only physical-closure observation for one original grant."""

    verified_absent: bool
    reason_code: str


@dataclass(frozen=True)
class GatedWindowRecoveryOutcome:
    """Observed result for one original grant; never actor-readiness authority."""

    window_id: str
    closure_verified: bool
    settlement: WindowSettlement | None
    quarantine_released: bool
    reason_code: str | None


@dataclass(frozen=True)
class GatedInstanceRecoveryReport:
    """Per-window recovery observations for one trusted Run instance."""

    windows: tuple[GatedWindowRecoveryOutcome, ...]


class GatedWindowRecoveryPort(Protocol):
    """Trusted host caller seam for read/observe/settle of original windows."""

    async def reconcile_instance(
        self,
        *,
        run_id: str,
        instance_id: str,
        endpoint: PreparedDockerEndpoint | None,
    ) -> GatedInstanceRecoveryReport:
        """Reconcile existing windows only; no holder identity is accepted.

        A missing/unbound endpoint yields conservative per-window UNKNOWN
        outcomes. A ledger read failure raises its structured port error so it
        cannot be confused with a successful empty inventory. The caller must
        hold the original Run-instance recovery ownership and prove process
        quiescence before calling; it must still use its ordinary recovery
        CAS/admission checks after this returns. This report is never
        actor-readiness authority.
        """
        ...
