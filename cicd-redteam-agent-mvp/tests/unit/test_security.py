from pathlib import Path

import pytest

from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import (
    RepositoryBoundary,
    RepositoryBoundaryError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_repository_boundary_rejects_escape_and_git() -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve("../outside")
    with pytest.raises(RepositoryBoundaryError):
        boundary.resolve(".git/config", must_exist=False)


def test_slice_limit() -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    with pytest.raises(RepositoryBoundaryError):
        boundary.read_slice("scripts/deploy.sh", 1, 201)


def test_redaction_preserves_reference_but_hides_value() -> None:
    value = "api_key=super-secret TOKEN: abc123 ${{ secrets.DEPLOY_TOKEN }}"
    result = redact(value)
    assert "super-secret" not in result
    assert "abc123" not in result
    assert "${{ secrets.DEPLOY_TOKEN }}" in result


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    boundary = RepositoryBoundary(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        boundary.read_text("large.txt")
