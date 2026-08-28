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
    known_relations: dict[str, list] = {}
    for relation in state.relations:
        known_relations.setdefault(relation.relation_id, []).append(relation)

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
        contains_unknown_relation = False
        contains_false_relation = False
        relation_missing_evidence: set[str] = set()
        edge_evidence: set[str] = set()
        for index, edge in enumerate(path.edges):
            if edge.relation_type == "can_use_github_token_id-token_write":
                errors.append(
                    f"AttackPath {path.path_id} treats id-token: write as a "
                    "GITHUB_TOKEN write sink; use the extracted "
                    "can_request_oidc_token relation for an OIDC path"
                )
            if index + 1 >= len(path.nodes) or (
                edge.source_id != path.nodes[index]
                or edge.target_id != path.nodes[index + 1]
            ):
                errors.append(
                    f"AttackPath {path.path_id} contains an edge outside its ordered sequence"
                )
            missing = set(edge.evidence_ids) - known_evidence
            edge_evidence.update(edge.evidence_ids)
            if missing:
                errors.append(f"AttackPath edge references unknown evidence: {sorted(missing)}")
            matches = known_relations.get(edge.relation_id, [])
            if not matches:
                errors.append(
                    f"AttackPath {path.path_id} references an unknown relation_id: "
                    f"{edge.relation_id}"
                )
                continue
            matching_shape = [
                relation
                for relation in matches
                if relation.source_id == edge.source_id
                and relation.relation_type == edge.relation_type
                and relation.target_id == edge.target_id
            ]
            if not matching_shape:
                errors.append(
                    f"AttackPath edge {edge.relation_id} does not match the extracted "
                    "relation source/type/target"
                )
                continue
            if not any(
                set(item.evidence_ids).issubset(edge.evidence_ids)
                for item in matching_shape
            ):
                required = sorted(
                    {ev for item in matching_shape for ev in item.evidence_ids}
                )
                errors.append(
                    f"AttackPath edge omits relation evidence for {edge.relation_id}: "
                    f"{required}"
                )
            for relation in matching_shape:
                if relation.verdict.value == "UNKNOWN":
                    contains_unknown_relation = True
                    relation_missing_evidence.update(relation.missing_evidence)
                elif relation.verdict.value == "FALSE":
                    contains_false_relation = True
        if path.status is FindingStatus.SUPPORTED and (
            contains_unknown_relation or contains_false_relation
        ):
            errors.append(
                f"AttackPath {path.path_id} cannot be SUPPORTED with UNKNOWN/FALSE relations"
            )
        if contains_false_relation and path.status is not FindingStatus.REJECTED:
            errors.append(
                f"AttackPath {path.path_id} with a FALSE relation must be REJECTED"
            )
        if path.status is FindingStatus.SUPPORTED and path.missing_evidence:
            errors.append(
                f"AttackPath {path.path_id} cannot be SUPPORTED with missing_evidence"
            )
        if not edge_evidence.issubset(path.evidence_ids):
            errors.append(
                f"AttackPath {path.path_id} omits edge evidence: "
                f"{sorted(edge_evidence - set(path.evidence_ids))}"
            )
        if relation_missing_evidence and not relation_missing_evidence.issubset(
            set(path.missing_evidence)
        ):
            errors.append(
                f"AttackPath {path.path_id} omits relation missing_evidence: "
                f"{sorted(relation_missing_evidence)}"
            )
    for finding in proposal.findings:
        if finding.status is FindingStatus.CONFIRMED:
            errors.append(f"Static prototype forbids CONFIRMED finding {finding.finding_id}")
        if finding.status is FindingStatus.SUPPORTED and finding.missing_evidence:
            errors.append(
                f"Finding {finding.finding_id} cannot be SUPPORTED with missing_evidence"
            )
        if finding.status is FindingStatus.SUPPORTED and not any(
            path.status is FindingStatus.SUPPORTED
            and bool(set(path.evidence_ids) & set(finding.evidence_ids))
            for path in proposal.paths
        ):
            errors.append(
                f"Finding {finding.finding_id} requires an evidence-linked SUPPORTED path"
            )
    for identity in proposal.identities:
        if identity.repository_id != state.repository.repository_id:
            errors.append(f"Cloud identity {identity.identity_id} references another repository")
        if identity.workflow_id not in known_workflows:
            errors.append(f"Cloud identity {identity.identity_id} references an unknown workflow")
        if identity.job_id not in known_jobs:
            errors.append(f"Cloud identity {identity.identity_id} references an unknown job")
        candidates = [
            item
            for item in state.identities
            if item.repository_id == identity.repository_id
            and item.workflow_id == identity.workflow_id
            and item.job_id == identity.job_id
            and item.provider == identity.provider
            and item.mechanism == identity.mechanism
        ]
        if not candidates:
            errors.append(
                f"Cloud identity {identity.identity_id} is not grounded in an extracted candidate"
            )
        elif identity.identity_id not in {item.identity_id for item in candidates}:
            errors.append(
                f"Cloud identity {identity.identity_id} changes an extracted candidate ID"
            )
        elif not any(
            set(candidate.evidence_ids).issubset(identity.evidence_ids)
            for candidate in candidates
        ):
            errors.append(
                f"Cloud identity {identity.identity_id} omits extracted identity evidence"
            )
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
