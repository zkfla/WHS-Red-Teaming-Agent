import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.models import AnalysisMode
from cicd_redteam_agent.framework.capabilities import (
    CapabilityContext,
    GetWorkflowFacts,
    InspectRepositoryFile,
    SearchRepository,
    SubmitAnalysis,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.asyncio
async def test_capabilities_return_structured_boundary_errors(tmp_path: Path) -> None:
    boundary = RepositoryBoundary(tmp_path)
    context = CapabilityContext(create_initial_state(boundary), boundary)

    inspect_result = json.loads(await InspectRepositoryFile(context)("../outside", 1, 1))
    search_result = json.loads(await SearchRepository(context)("", None, 50))

    assert inspect_result["error"] == "repository_boundary_error"
    assert search_result["error"] == "repository_boundary_error"
    assert context.state.evidence == {}


@pytest.mark.asyncio
async def test_malformed_and_duplicate_submit_do_not_mutate_state(tmp_path: Path) -> None:
    boundary = RepositoryBoundary(tmp_path)
    context = CapabilityContext(create_initial_state(boundary), boundary)
    capability = SubmitAnalysis(context)

    malformed = json.loads(await capability("not-json"))
    accepted = json.loads(await capability('{"schema_version":"1.0"}'))
    duplicate = json.loads(await capability('{"schema_version":"1.0"}'))

    assert not malformed["accepted"]
    assert context.state.findings == []
    assert accepted["accepted"]
    assert not duplicate["accepted"]
    assert duplicate["errors"] == ["analysis_already_submitted"]


@pytest.mark.asyncio
async def test_workflow_facts_expose_deterministic_graph_context(tmp_path: Path) -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    report, _ = await analyze_repository(
        boundary.root, AnalysisConfig(output_dir=tmp_path / "output")
    )
    context = CapabilityContext(report.state, boundary)
    workflow_id = report.state.workflows[0].workflow_id

    result = json.loads(await GetWorkflowFacts(context)(workflow_id))
    unknown = json.loads(await GetWorkflowFacts(context)("workflow_missing"))

    assert result["assessments"]
    assert result["findings"]
    assert result["attack_paths"]
    assert result["identities"]
    assert unknown["error"] == "unknown_workflow_id"

    reasoning = CapabilityContext(
        report.state,
        boundary,
        analysis_mode=AnalysisMode.REASONING,
    )
    reasoning_result = json.loads(await GetWorkflowFacts(reasoning)(workflow_id))
    assert "findings" not in reasoning_result
    assert "attack_paths" not in reasoning_result
