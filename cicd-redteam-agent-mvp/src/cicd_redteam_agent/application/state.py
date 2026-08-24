from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime

from cicd_redteam_agent.domain.models import AnalysisState, RepositorySnapshot
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def _git_value(boundary: RepositoryBoundary, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=boundary.root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def create_initial_state(boundary: RepositoryBoundary) -> AnalysisState:
    commit = _git_value(boundary, "rev-parse", "HEAD")
    status = _git_value(boundary, "status", "--porcelain")
    repository_id = "repo_" + hashlib.sha256(str(boundary.root).encode()).hexdigest()[:16]
    return AnalysisState(
        repository=RepositorySnapshot(
            repository_id=repository_id,
            root=str(boundary.root),
            git_commit=commit,
            dirty=None if status is None else bool(status),
            analyzed_at=datetime.now(UTC),
        )
    )
