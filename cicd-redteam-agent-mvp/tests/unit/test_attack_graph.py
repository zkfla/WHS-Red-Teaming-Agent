from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.models import FindingStatus

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.asyncio
async def test_global_paths_join_artifact_execution_to_credentials(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "global-chain", AnalysisConfig(output_dir=tmp_path)
    )
    global_paths = [
        path for path in report.state.paths if path.nodes[0].startswith("attacker:")
    ]

    assert any(
        path.nodes[-1].startswith("credential:github-token:")
        and path.status is FindingStatus.SUPPORTED
        for path in global_paths
    )
    assert any(
        path.nodes[-1].startswith("identity_")
        and path.status is FindingStatus.CANDIDATE
        and "cloud identity trust policy" in path.missing_evidence
        for path in global_paths
    )
    assert all(
        edge.source_id == path.nodes[index]
        and edge.target_id == path.nodes[index + 1]
        for path in global_paths
        for index, edge in enumerate(path.edges)
    )
    assert all(
        evidence_id in report.state.evidence
        for path in global_paths
        for evidence_id in path.evidence_ids
    )


@pytest.mark.asyncio
async def test_mutable_action_without_privilege_is_not_a_finding(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "build.yml").write_text(
        """name: Mutable but unprivileged
on: push
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: example-org/build-action@v1
""",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "output"),
    )
    assert not any(
        finding.rule_id == "GHA-04-MUTABLE-EXTERNAL-ACTION"
        for finding in report.state.findings
    )


@pytest.mark.asyncio
async def test_trigger_alone_does_not_reach_a_write_token(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "check.yml").write_text(
        """name: Trigger only
on: pull_request
permissions: write-all
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo fixed
""",
        encoding="utf-8",
    )

    report, _ = await analyze_repository(
        repository, AnalysisConfig(output_dir=tmp_path / "output")
    )

    assert not any(path.nodes[0].startswith("attacker:") for path in report.state.paths)
