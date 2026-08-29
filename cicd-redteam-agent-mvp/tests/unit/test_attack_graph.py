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
async def test_mutable_action_findings_are_aggregated_by_workflow_root_cause(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "mutable-action-aggregation"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "build.yml").write_text(
        """name: Mutable aggregation
on: push
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: example-org/build-action@v1
      - uses: example-org/build-action@v1
""",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "mutable-output"),
    )
    findings = [
        finding
        for finding in report.state.findings
        if finding.rule_id == "GHA-04-MUTABLE-EXTERNAL-ACTION"
    ]
    assert len(findings) == 1
    assert "used 2 time(s)" in findings[0].summary


@pytest.mark.asyncio
async def test_secret_to_log_relation_is_promoted_without_claiming_masking_failure(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "reasoning-matrix" / "gha-07" / "supported",
        AnalysisConfig(output_dir=tmp_path / "secret-output"),
    )
    findings = [
        finding
        for finding in report.state.findings
        if finding.rule_id == "GHA-07-SECRET-MAY-REACH-LOG"
    ]
    assert findings
    assert all(finding.status is FindingStatus.SUPPORTED for finding in findings)
    assert all(
        "does not claim that GitHub masking failed" in finding.summary
        for finding in findings
    )


@pytest.mark.asyncio
async def test_self_hosted_persistence_relation_is_promoted_as_candidate(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "reasoning-matrix" / "gha-09" / "supported",
        AnalysisConfig(output_dir=tmp_path / "runner-output"),
    )
    findings = [
        finding
        for finding in report.state.findings
        if finding.rule_id == "GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE"
    ]
    assert findings
    assert all(finding.status is FindingStatus.CANDIDATE for finding in findings)
    assert any("runner cleanup" in finding.missing_evidence for finding in findings)


@pytest.mark.asyncio
async def test_label_approval_plus_current_pr_head_execution_is_toctou_candidate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "revision-toctou"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "deploy.yml").write_text(
        """name: Revision TOCTOU
on: pull_request_target
permissions: write-all
jobs:
  deploy:
    if: contains(github.event.pull_request.labels.*.name, 'safe-to-test')
    runs-on: ubuntu-latest
    steps:
      - run: gh pr checkout ${{ github.event.pull_request.number }}
      - run: npm test
""",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "toctou-output"),
    )
    relation_types = {relation.relation_type for relation in report.state.relations}
    assert "attacker_selects_current_pull_request_head" in relation_types
    assert "workspace_revision_reaches_execution" in relation_types
    findings = [
        finding
        for finding in report.state.findings
        if finding.rule_id == "GHA-10-PR-REVISION-TOCTOU"
    ]
    assert len(findings) == 1
    assert findings[0].status is FindingStatus.CANDIDATE
    assert "exact approval-to-execution revision binding" in findings[0].missing_evidence


@pytest.mark.asyncio
async def test_event_head_sha_does_not_create_mutable_head_toctou_finding(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "revision-bound"
    workflow_dir = repository / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "deploy.yml").write_text(
        """name: Revision bound
on: pull_request_target
permissions: write-all
jobs:
  deploy:
    if: contains(github.event.pull_request.labels.*.name, 'safe-to-test')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: npm test
""",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "bound-output"),
    )
    assert any(
        relation.relation_type == "attacker_selects_event_pull_request_head_sha"
        for relation in report.state.relations
    )
    assert not any(
        finding.rule_id == "GHA-10-PR-REVISION-TOCTOU"
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


@pytest.mark.asyncio
async def test_cache_poisoning_reaches_privileged_token_sink(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "reasoning-matrix" / "gha-08" / "supported",
        AnalysisConfig(output_dir=tmp_path),
    )

    cache_paths = [
        path
        for path in report.state.paths
        if path.nodes[0] == "attacker:fork-pr"
        and path.nodes[-1].startswith("credential:github-token:")
        and "cache_restores_executable_or_dependency_path"
        in {edge.relation_type for edge in path.edges}
    ]

    assert cache_paths
    assert any(path.status is FindingStatus.SUPPORTED for path in cache_paths)
    assert all(
        path.status is FindingStatus.SUPPORTED
        or "repository default GITHUB_TOKEN permissions" in path.missing_evidence
        for path in cache_paths
    )


@pytest.mark.asyncio
async def test_mutable_action_is_candidate_path_to_token(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "gha-04-mutable-action" / "vulnerable",
        AnalysisConfig(output_dir=tmp_path),
    )

    paths = [
        path
        for path in report.state.paths
        if path.nodes[0] == "attacker:external-dependency"
        and path.nodes[-1].startswith("credential:github-token:")
    ]

    assert paths
    assert all(path.status is FindingStatus.CANDIDATE for path in paths)
    assert all("external dependency compromise" in path.missing_evidence for path in paths)


@pytest.mark.asyncio
async def test_oidc_path_preserves_unknown_cloud_trust(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "oidc-risk",
        AnalysisConfig(output_dir=tmp_path),
    )

    paths = [
        path
        for path in report.state.paths
        if path.nodes[0] == "attacker:fork-pr"
        and "oidc_may_assume_cloud_identity"
        in {edge.relation_type for edge in path.edges}
    ]

    assert paths
    assert all(path.status is FindingStatus.CANDIDATE for path in paths)
    assert all(
        any("trust policy" in item for item in path.missing_evidence) for path in paths
    )
