"""Investigation-path store port owned by Worker A."""

from __future__ import annotations

from typing import Protocol

from redteam.orchestration.paths.models import PathPlan


class PathStore(Protocol):
    """Versioned path snapshots plus per-version step progress."""

    async def get_active(self, run_id: str) -> PathPlan | None:
        """Return the active path version for ``run_id`` merged with progress."""
        ...

    async def list_paths(self, run_id: str) -> list[PathPlan]:
        """Return every stored version for ``run_id`` merged with progress."""
        ...

    async def register(
        self,
        path: PathPlan,
        expected_version: int | None = None,
        *,
        expected_path_id: str | None = None,
    ) -> PathPlan:
        """Store one version snapshot, optionally moving the active pointer."""
        ...

    async def update_step(
        self,
        run_id: str,
        path_id: str,
        version: int,
        step_id: str,
        status: str,
        outcome: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> PathPlan | None:
        """Update progress for one step of the active path version."""
        ...
