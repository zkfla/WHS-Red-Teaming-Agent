from __future__ import annotations

from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AnalysisState,
    CandidateKind,
    FindingStatus,
    ValidationResult,
)
from cicd_redteam_agent.domain.relation_semantics import relation_semantics

_STATUS_RANK = {
    FindingStatus.REJECTED: 0,
    FindingStatus.CANDIDATE: 1,
    FindingStatus.SUPPORTED: 2,
    FindingStatus.CONFIRMED: 3,
}

_NOVEL_CLAIM_CONTRACTS = {
    "NOVEL-UNTRUSTED-EXECUTION": {
        "source_tags": {"pull_request", "repository_writer", "artifact", "cache"},
        "mechanism_tags": {
            "shell_execution",
            "artifact",
            "cache",
            "self_hosted_runner",
            "mutable_dependency",
            "revision_checkout",
        },
        "sink_tags": {"execution"},
        "ceiling": FindingStatus.SUPPORTED,
    },
    "NOVEL-SECRET-EXPOSURE": {
        "source_tags": {"pull_request", "repository_writer", "secret"},
        "mechanism_tags": {"secret", "shell_execution"},
        "sink_tags": {"secret_exposure"},
        "ceiling": FindingStatus.CANDIDATE,
    },
    "NOVEL-CREDENTIAL-ACCESS": {
        "source_tags": {"pull_request", "repository_writer", "artifact", "cache"},
        "mechanism_tags": {
            "permissions",
            "oidc",
            "artifact",
            "cache",
            "shell_execution",
        },
        "sink_tags": {"github_token_write", "oidc_token", "cloud_identity"},
        "ceiling": FindingStatus.CANDIDATE,
    },
}

_SECURITY_CLAIM_TERMS = (
    "vulnerab",
    "risk",
    "injection",
    "exposure",
    "execution",
    "credential",
    "token",
    "secret",
    "oidc",
    "privileged",
    "supply-chain",
    "supply chain",
    "persistence",
    "toctou",
    "advisory",
    "compromise",
)


def novel_claim_contracts() -> list[dict[str, object]]:
    """Expose the finite typed contract vocabulary to the reasoning capability."""

    return [
        {
            "rule_id": rule_id,
            "required_source_tags": sorted(contract["source_tags"]),
            "required_mechanism_tags": sorted(contract["mechanism_tags"]),
            "required_sink_tags": sorted(contract["sink_tags"]),
            "proposal_status_ceiling": contract["ceiling"].value,
        }
        for rule_id, contract in sorted(_NOVEL_CLAIM_CONTRACTS.items())
    ]


def _candidate_relation_ids(path) -> set[str]:
    return {edge.relation_id for edge in path.edges}


def _matches_canonical_candidate_variant(path, candidate) -> bool:
    path_edges = tuple(
        (edge.source_id, edge.relation_type, edge.target_id, edge.relation_id)
        for edge in path.edges
    )
    return any(
        path.path_id == variant.path_id
        and path_edges
        == tuple(
            (edge.source_id, edge.relation_type, edge.target_id, edge.relation_id)
            for edge in variant.edges
        )
        for variant in candidate.path_variants
    )


def _explicit_privilege_relation(relation_type: str) -> bool:
    return (
        relation_type.startswith("can_use_github_token_")
        and relation_type.endswith("_write")
        and relation_type != "can_use_github_token_id-token_write"
    ) or relation_type in {"can_request_oidc_token", "can_access_secret_reference"}


def _novel_contract_error(
    finding,
    proposal_paths,
    relation_by_id,
) -> str | None:
    claim_text = f"{finding.rule_id} {finding.title} {finding.summary}".casefold()
    if not any(term in claim_text for term in _SECURITY_CLAIM_TERMS):
        return None
    contract = _NOVEL_CLAIM_CONTRACTS.get(finding.rule_id)
    if contract is None:
        return (
            f"Finding {finding.finding_id} requires a canonical candidate_id or a known "
            "typed novel claim contract"
        )
    if not proposal_paths:
        return f"Finding {finding.finding_id} requires a path for its typed claim contract"
    relation_types = {
        relation_by_id[edge.relation_id].relation_type
        for path in proposal_paths
        for edge in path.edges
        if edge.relation_id in relation_by_id
    }
    semantics = [relation_semantics(relation_type) for relation_type in relation_types]
    source_tags = {item.source_tag for item in semantics if item.source_tag is not None}
    mechanism_tags = {
        tag for item in semantics for tag in item.mechanism_tags
    }
    sink_tags = {item.sink_tag for item in semantics if item.sink_tag is not None}
    missing = []
    if not source_tags & contract["source_tags"]:
        missing.append("source")
    if not mechanism_tags & contract["mechanism_tags"]:
        missing.append("mechanism")
    if not sink_tags & contract["sink_tags"]:
        missing.append("sink")
    if missing:
        return (
            f"Finding {finding.finding_id} does not satisfy typed novel claim contract "
            f"{finding.rule_id}: missing {', '.join(missing)} semantics"
        )
    ceiling = contract["ceiling"]
    if _STATUS_RANK[finding.status] > _STATUS_RANK[ceiling]:
        return (
            f"Finding {finding.finding_id} exceeds typed novel claim status ceiling "
            f"{ceiling.value}"
        )
    return None


def _forbidden_claim_used(text: str, phrase: str) -> bool:
    """Reject affirmative overclaims while allowing an explicit negated warning."""

    start = 0
    negative_markers = (
        "does not claim",
        "doesn't claim",
        "not claim",
        "not observed",
        "no evidence",
        "without evidence",
    )
    while (position := text.find(phrase.casefold(), start)) >= 0:
        prefix = text[max(0, position - 80) : position]
        if not any(marker in prefix for marker in negative_markers):
            return True
        start = position + len(phrase)
    return False


def validate_candidate_claims(
    state: AnalysisState,
    proposal: AnalysisProposal,
) -> list[str]:
    """Validate semantic claim ceilings in addition to structural path grounding."""

    errors: list[str] = []
    candidates = {candidate.candidate_id: candidate for candidate in state.candidates}
    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    for path in proposal.paths:
        if path.candidate_id is None:
            continue
        candidate = candidates.get(path.candidate_id)
        if candidate is None:
            errors.append(f"AttackPath {path.path_id} references unknown candidate_id")
            continue
        relation_ids = _candidate_relation_ids(path)
        allowed_relation_ids = set(candidate.supporting_relation_ids) | set(
            candidate.contradicting_relation_ids
        )
        allowed_relation_ids.update(
            edge.relation_id
            for variant in candidate.path_variants
            for edge in variant.edges
        )
        if not relation_ids <= allowed_relation_ids:
            errors.append(
                f"AttackPath {path.path_id} contains relations outside candidate "
                f"{candidate.candidate_id}"
            )
        if _STATUS_RANK[path.status] > _STATUS_RANK[candidate.proposal_status_ceiling]:
            errors.append(
                f"AttackPath {path.path_id} exceeds candidate status ceiling "
                f"{candidate.proposal_status_ceiling.value}"
            )
        if candidate.candidate_kind is CandidateKind.UNTRUSTED_PRIVILEGED_EXECUTION:
            privilege_relations = [
                relation_by_id[relation_id]
                for relation_id in relation_ids
                if relation_id in relation_by_id
                and _explicit_privilege_relation(relation_by_id[relation_id].relation_type)
            ]
            if path.status is FindingStatus.SUPPORTED and not any(
                relation.verdict.value == "TRUE" for relation in privilege_relations
            ):
                errors.append(
                    f"AttackPath {path.path_id} cannot claim privileged SUPPORTED execution "
                    "without an explicit TRUE privilege relation"
                )

    for finding in proposal.findings:
        candidate = (
            candidates.get(finding.candidate_id)
            if finding.candidate_id is not None
            else None
        )
        if finding.candidate_id is not None and candidate is None:
            errors.append(f"Finding {finding.finding_id} references unknown candidate_id")
            continue
        if candidate is None:
            continue
        contract = candidate.claim_contract
        if finding.rule_id != contract.rule_id:
            errors.append(
                f"Finding {finding.finding_id} rule_id is outside candidate claim contract"
            )
        if finding.title != contract.title:
            errors.append(
                f"Finding {finding.finding_id} title is outside candidate claim contract"
            )
        if finding.summary != contract.bounded_summary:
            errors.append(
                f"Finding {finding.finding_id} summary is outside candidate claim contract"
            )
        if finding.status is not candidate.status:
            errors.append(
                f"Finding {finding.finding_id} status does not match candidate disposition"
            )
        if _STATUS_RANK[finding.status] > _STATUS_RANK[candidate.proposal_status_ceiling]:
            errors.append(
                f"Finding {finding.finding_id} exceeds candidate status ceiling "
                f"{candidate.proposal_status_ceiling.value}"
            )
        if not set(candidate.evidence_ids).issubset(finding.evidence_ids):
            errors.append(
                f"Finding {finding.finding_id} omits candidate evidence grounding"
            )
        if not set(candidate.missing_evidence).issubset(finding.missing_evidence):
            errors.append(
                f"Finding {finding.finding_id} omits candidate missing evidence"
            )
        lowered = f"{finding.title} {finding.summary}".casefold()
        forbidden = [
            phrase
            for phrase in contract.forbidden_claims
            if _forbidden_claim_used(lowered, phrase)
        ]
        if forbidden:
            errors.append(
                f"Finding {finding.finding_id} uses forbidden claim language: {sorted(forbidden)}"
            )
        if candidate.source_finding_id != finding.finding_id:
            # This branch is intentionally informationally strict only for candidate-backed
            # findings; legacy novel findings remain available through the neutral channel.
            errors.append(
                f"Finding {finding.finding_id} does not match candidate source identity"
            )
        related_paths = [
            path
            for path in proposal.paths
            if path.candidate_id == candidate.candidate_id
        ]
        if not related_paths:
            errors.append(
                f"Finding {finding.finding_id} requires a path for candidate "
                f"{candidate.candidate_id}"
            )
    for finding in proposal.findings:
        if finding.candidate_id is not None:
            continue
        error = _novel_contract_error(
            finding,
            list(proposal.paths),
            relation_by_id,
        )
        if error is not None:
            errors.append(error)

    # Legacy/novel proposals still get a hard negative when they try to turn mere repository
    # execution into privileged execution without an explicit credential or write relation.
    for finding in proposal.findings:
        claim_text = f"{finding.rule_id} {finding.title} {finding.summary}".casefold()
        if "privileged" not in claim_text and "elevated" not in claim_text:
            continue
        candidate = candidates.get(finding.candidate_id) if finding.candidate_id else None
        candidate_paths = [
            path
            for path in proposal.paths
            if finding.candidate_id is not None
            and path.candidate_id == finding.candidate_id
        ]
        if not candidate_paths:
            candidate_paths = list(proposal.paths)
        relation_types = {
            relation_by_id[edge.relation_id].relation_type
            for path in candidate_paths
            for edge in path.edges
            if edge.relation_id in relation_by_id
        }
        explicit = {
            relation_type
            for relation_type in relation_types
            if _explicit_privilege_relation(relation_type)
        }
        has_true_privilege = any(
            relation_by_id[relation_id].verdict.value == "TRUE"
            for path in candidate_paths
            for relation_id in _candidate_relation_ids(path)
            if relation_id in relation_by_id
            and _explicit_privilege_relation(relation_by_id[relation_id].relation_type)
        )
        if finding.status is FindingStatus.SUPPORTED and not has_true_privilege:
            errors.append(
                f"Finding {finding.finding_id} cannot claim privileged execution without "
                "an explicit TRUE privilege relation"
            )
        if candidate is None and finding.status is FindingStatus.SUPPORTED and not explicit:
            errors.append(
                f"Finding {finding.finding_id} has no canonical privileged candidate"
            )
    return errors


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
    known_candidates = {candidate.candidate_id: candidate for candidate in state.candidates}

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
                canonical_candidate = known_candidates.get(path.candidate_id)
                if canonical_candidate is not None and _matches_canonical_candidate_variant(
                    path,
                    canonical_candidate,
                ):
                    # Scenario composers can carry a bounded semantic edge that is not a raw
                    # extractor Relation. Candidate binding makes that edge non-forgeable while
                    # preserving the normal evidence/node/path checks above.
                    continue
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
    errors.extend(validate_candidate_claims(state, proposal))
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
