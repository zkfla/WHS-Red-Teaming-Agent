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
    credential_id = "credential:github-token:wf_123:job:deploy"
    value = (
        "api_key=super-secret TOKEN: abc123 ${{ secrets.DEPLOY_TOKEN }} "
        f"{credential_id}"
    )
    result = redact(value)
    assert "super-secret" not in result
    assert "abc123" not in result
    assert "${{ secrets.DEPLOY_TOKEN }}" in result
    assert credential_id in result


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    boundary = RepositoryBoundary(tmp_path)
    with pytest.raises(RepositoryBoundaryError):
        boundary.read_text("large.txt")


def test_binary_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"prefix\x00suffix")
    boundary = RepositoryBoundary(tmp_path)

    with pytest.raises(RepositoryBoundaryError):
        boundary.read_text("binary.dat")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = repository / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows host")

    boundary = RepositoryBoundary(repository)
    with pytest.raises(RepositoryBoundaryError):
        boundary.read_text("link.txt")


def test_search_is_literal_redacted_and_capped(tmp_path: Path) -> None:
    lines = [f"token=secret-{index} marker" for index in range(60)]
    (tmp_path / "data.txt").write_text("\n".join(lines), encoding="utf-8")
    boundary = RepositoryBoundary(tmp_path)

    results = boundary.search_literal("marker", max_results=500)

    assert len(results) == 50
    assert all("secret-" not in str(item["excerpt"]) for item in results)
