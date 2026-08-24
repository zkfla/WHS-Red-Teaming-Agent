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
    known_workflows: set[str] = set()
    known_jobs: set[str] = set()
    for workflow in state.workflows:
        known_workflows.add(workflow.workflow_id)
        known_nodes.add(workflow.workflow_id)
        for job in workflow.jobs:
            known_jobs.add(job.job_id)
            known_nodes.add(job.job_id)
            known_nodes.update(step.step_id for step in job.steps)
    known_nodes.update(relation.source_id for relation in state.relations)
    known_nodes.update(relation.target_id for relation in state.relations)
    known_nodes.update(identity.identity_id for identity in state.identities)
    for path in state.paths:
        known_nodes.update(path.nodes)

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
        if len(path.nodes) < 2:
            errors.append(f"AttackPath {path.path_id} must contain at least two ordered nodes")
        if len(path.edges) != max(0, len(path.nodes) - 1):
            errors.append(
                f"AttackPath {path.path_id} must have exactly one edge between ordered nodes"
            )
        unknown_nodes = set(path.nodes) - known_nodes
        if unknown_nodes:
            errors.append(f"AttackPath {path.path_id} has unknown nodes: {sorted(unknown_nodes)}")
        for index, edge in enumerate(path.edges):
            if index + 1 >= len(path.nodes) or (
                edge.source_id != path.nodes[index]
                or edge.target_id != path.nodes[index + 1]
            ):
                errors.append(
                    f"AttackPath {path.path_id} contains an edge outside its ordered sequence"
                )
            missing = set(edge.evidence_ids) - known_evidence
            if missing:
                errors.append(f"AttackPath edge references unknown evidence: {sorted(missing)}")
    for identity in proposal.identities:
        if identity.repository_id != state.repository.repository_id:
            errors.append(f"Cloud identity {identity.identity_id} references another repository")
        if identity.workflow_id not in known_workflows:
            errors.append(f"Cloud identity {identity.identity_id} references an unknown workflow")
        if identity.job_id not in known_jobs:
            errors.append(f"Cloud identity {identity.identity_id} references an unknown job")
    if len({item.finding_id for item in proposal.findings}) != len(proposal.findings):
        errors.append("Duplicate finding IDs")
    if len({item.path_id for item in proposal.paths}) != len(proposal.paths):
        errors.append("Duplicate path IDs")
    if len({item.identity_id for item in proposal.identities}) != len(proposal.identities):
        errors.append("Duplicate identity IDs")
    existing_finding_ids = {item.finding_id for item in state.findings}
    existing_path_ids = {item.path_id for item in state.paths}
    existing_identity_ids = {item.identity_id for item in state.identities}
    if collision := existing_finding_ids & {item.finding_id for item in proposal.findings}:
        errors.append(f"Finding IDs already exist: {sorted(collision)}")
    if collision := existing_path_ids & {item.path_id for item in proposal.paths}:
        errors.append(f"AttackPath IDs already exist: {sorted(collision)}")
    if collision := existing_identity_ids & {item.identity_id for item in proposal.identities}:
        errors.append(f"Cloud identity IDs already exist: {sorted(collision)}")
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
