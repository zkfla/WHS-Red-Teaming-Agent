from pathlib import Path

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.attacker_control import assess_attacker_control, derive_attack_paths
from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.domain.identity_analysis import derive_identity_candidates
from cicd_redteam_agent.domain.models import AttackerProfile, FindingStatus, TriState
from cicd_redteam_agent.domain.reference_tracer import trace_local_references
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


def build_state():
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    state = create_initial_state(boundary)
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    state.workflows = [result.workflow]
    state.evidence.update(result.evidence)
    state.secret_references = result.secret_references
    relations, evidence, _ = trace_local_references(state.workflows, boundary)
    state.relations = relations
    state.evidence.update(evidence)
    return state


def test_fork_pr_supported_path_and_unknown_cloud_trust() -> None:
    state = build_state()
    state.assessments = assess_attacker_control(state, AttackerProfile.FORK_PR)
    assert any(item.verdict is TriState.TRUE for item in state.assessments)
    state.findings, state.paths = derive_attack_paths(state)
    assert state.paths[0].status is FindingStatus.SUPPORTED
    state.identities = derive_identity_candidates(state)
    assert state.identities[0].oidc_enabled is TriState.TRUE
    assert state.identities[0].trust_verified is TriState.UNKNOWN


def test_local_composite_action_is_traced_without_execution() -> None:
    boundary = RepositoryBoundary(FIXTURES / "local-action")
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    relations, evidence, warnings = trace_local_references([result.workflow], boundary)
    targets = {item.target_id for item in relations}
    assert "file:actions/check/action.yml" in targets
    assert "file:actions/check/check.sh" in targets
    assert evidence
    assert not warnings


def test_local_reusable_workflow_and_its_script_are_traced() -> None:
    boundary = RepositoryBoundary(FIXTURES / "local-reusable")
    caller = parse_workflow(boundary, ".github/workflows/caller.yml")
    relations, _, warnings = trace_local_references([caller.workflow], boundary)
    targets = {item.target_id for item in relations}
    assert "file:.github/workflows/reusable.yml" in targets
    assert "file:scripts/check.sh" in targets
    assert not warnings


def _assess_fixture(name: str):
    boundary = RepositoryBoundary(FIXTURES / name)
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    state = create_initial_state(boundary)
    state.workflows = [result.workflow]
    state.evidence.update(result.evidence)
    state.relations, reference_evidence, _ = trace_local_references(state.workflows, boundary)
    state.evidence.update(reference_evidence)
    return assess_attacker_control(state, AttackerProfile.FORK_PR)


def test_pull_request_target_checkout_provenance_is_tri_state() -> None:
    unknown = _assess_fixture("pr-target-unknown")
    controlled = _assess_fixture("pr-target-controlled")
    assert unknown[0].verdict is TriState.UNKNOWN
    assert controlled[0].verdict is TriState.TRUE


def test_reference_cycle_is_bounded() -> None:
    boundary = RepositoryBoundary(FIXTURES / "cycle")
    result = parse_workflow(boundary, discover_workflows(boundary)[0])
    relations, _, _ = trace_local_references([result.workflow], boundary)
    assert any(item.relation_type == "local_reference_cycle" for item in relations)
