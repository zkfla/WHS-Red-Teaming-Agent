from __future__ import annotations

from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AnalysisState,
    FindingStatus,
    ValidationResult,
)


def validate_analysis_proposal(
    state: AnalysisState, proposal: AnalysisProposal
) -> ValidationResult:
    errors: list[str] = []
    known_evidence = set(state.evidence)
    known_nodes = {state.repository.repository_id}
    for workflow in state.workflows:
        known_nodes.add(workflow.workflow_id)
        for job in workflow.jobs:
            known_nodes.add(job.job_id)
            known_nodes.update(step.step_id for step in job.steps)
    known_nodes.update(relation.target_id for relation in state.relations)
    known_nodes.update(identity.identity_id for identity in state.identities)

    objects = [*proposal.findings, *proposal.paths, *proposal.identities]
    for item in objects:
        missing = set(item.evidence_ids) - known_evidence
        if missing:
            errors.append(
                f"{item.__class__.__name__} references unknown evidence: {sorted(missing)}"
            )
    for path in proposal.paths:
        if path.status is FindingStatus.CONFIRMED:
            errors.append(f"Static prototype forbids CONFIRMED attack path {path.path_id}")
        unknown_nodes = set(path.nodes) - known_nodes
        if unknown_nodes:
            errors.append(f"AttackPath {path.path_id} has unknown nodes: {sorted(unknown_nodes)}")
        for edge in path.edges:
            if edge.source_id not in path.nodes or edge.target_id not in path.nodes:
                errors.append(
                    f"AttackPath {path.path_id} contains an edge outside its ordered nodes"
                )
            missing = set(edge.evidence_ids) - known_evidence
            if missing:
                errors.append(f"AttackPath edge references unknown evidence: {sorted(missing)}")
    if len({item.finding_id for item in proposal.findings}) != len(proposal.findings):
        errors.append("Duplicate finding IDs")
    if len({item.path_id for item in proposal.paths}) != len(proposal.paths):
        errors.append("Duplicate path IDs")
    return ValidationResult(accepted=not errors, errors=errors)


def apply_analysis_proposal(state: AnalysisState, proposal: AnalysisProposal) -> ValidationResult:
    result = validate_analysis_proposal(state, proposal)
    if not result.accepted:
        return result
    state.findings = list(
        {item.finding_id: item for item in [*state.findings, *proposal.findings]}.values()
    )
    state.paths = list({item.path_id: item for item in [*state.paths, *proposal.paths]}.values())
    state.identities = list(
        {item.identity_id: item for item in [*state.identities, *proposal.identities]}.values()
    )
    return result
