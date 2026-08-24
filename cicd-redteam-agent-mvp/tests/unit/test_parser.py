from pathlib import Path

from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_yaml_12_and_permissions_and_secret_reference() -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    paths = discover_workflows(boundary)
    assert paths == [".github/workflows/deploy.yml"]
    result = parse_workflow(boundary, paths[0])
    assert "pull_request" in result.workflow.triggers
    job = result.workflow.jobs[0]
    assert job.permissions_source == "workflow"
    assert job.permissions_effective["id-token"] == "write"
    assert len(result.secret_references) == 1
    assert result.secret_references[0].expression == "${{ secrets.DEPLOY_TOKEN }}"
    assert all(item.line_start >= 1 for item in result.evidence.values())


def test_malformed_yaml_is_a_warning_not_a_crash() -> None:
    boundary = RepositoryBoundary(FIXTURES / "malformed")
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    assert result.workflow.parse_warnings
    assert any(item.kind == "parse_error" for item in result.evidence.values())


def test_job_permission_override_replaces_workflow_permissions() -> None:
    boundary = RepositoryBoundary(FIXTURES / "permission-override")
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    job = result.workflow.jobs[0]
    assert job.permissions_source == "job"
    assert job.permissions_effective == {"contents": "read"}


def test_dynamic_reusable_path_remains_unknown() -> None:
    boundary = RepositoryBoundary(FIXTURES / "dynamic-path")
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    assert any("UNKNOWN" in warning for warning in result.workflow.parse_warnings)
