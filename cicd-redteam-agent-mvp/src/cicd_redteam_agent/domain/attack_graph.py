from __future__ import annotations

from dataclasses import dataclass

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackEdge,
    AttackerProfile,
    AttackPath,
    FindingStatus,
    Relation,
    TriState,
)

MAX_GLOBAL_PATH_EDGES = 10
MAX_GLOBAL_PATHS = 50
COMPOSABLE_LOCAL_RELATIONS = {
    "uploads_pr_artifact",
    "triggers_privileged_workflow_run",
    "downloads_artifact",
    "artifact_file_executed_by",
}
COMPOSABLE_SECURITY_RELATIONS = {
    "controls_workflow_input",
    "value_flows_to_env",
    "env_value_flows_to_step_output",
    "env_value_interpreted_as_command",
    "output_value_interpreted_as_command",
    "untrusted_value_interpreted_as_command",
    "untrusted_input_reaches_execution",
    "controls_repository_file",
    "repository_file_executes_in",
    "attacker_may_control_artifact_content",
    "artifact_content_reaches_execution",
    "artifact_content_reaches_import",
    "attacker_may_populate_cache",
    "cache_restores_executable_or_dependency_path",
    "can_request_oidc_token",
    "oidc_may_assume_cloud_identity",
    "external_code_may_execute_in_step",
    "mutable_reusable_workflow_may_execute_in_job",
    "mutable_container_image_executes_in_job",
    "mutable_install_source_executes_in_step",
}
EXTERNAL_DEPENDENCY_RELATIONS = {
    "external_code_may_execute_in_step",
    "mutable_reusable_workflow_may_execute_in_job",
    "mutable_container_image_executes_in_job",
    "mutable_install_source_executes_in_step",
}
GENERIC_SINK_RELATIONS = {"oidc_may_assume_cloud_identity"}


@dataclass
class _EdgeFact:
    edge: AttackEdge
    status: FindingStatus
    missing_evidence: set[str]


def _supported(status: FindingStatus) -> bool:
    return status is FindingStatus.SUPPORTED


def _credential_write_enabled(value: dict[str, str] | str | None) -> bool:
    if value == "write-all":
        return True
    return isinstance(value, dict) and any(
        str(permission).lower() == "write" for permission in value.values()
    )


def compose_global_attack_paths(
    state: AnalysisState, profile: AttackerProfile
) -> tuple[list[Relation], list[AttackPath]]:
    """Join repository-backed local path facts into bounded source-to-credential paths."""
    facts: dict[tuple[str, str, str], _EdgeFact] = {}
    graph_relations: dict[tuple[str, str, str], Relation] = {}

    def register(
        source_id: str,
        relation_type: str,
        target_id: str,
        evidence_ids: list[str],
        status: FindingStatus = FindingStatus.SUPPORTED,
        missing_evidence: list[str] | None = None,
        *,
        expose_relation: bool = False,
    ) -> None:
        key = (source_id, relation_type, target_id)
        evidence = sorted(set(evidence_ids))
        missing = set(missing_evidence or [])
        existing = facts.get(key)
        if existing is None:
            facts[key] = _EdgeFact(
                edge=AttackEdge(
                    source_id=source_id,
                    relation_type=relation_type,
                    target_id=target_id,
                    evidence_ids=evidence,
                ),
                status=status,
                missing_evidence=missing,
            )
        else:
            existing.edge.evidence_ids = sorted(set(existing.edge.evidence_ids + evidence))
            if _supported(status):
                existing.status = FindingStatus.SUPPORTED
                existing.missing_evidence.clear()
            elif not _supported(existing.status):
                existing.missing_evidence.update(missing)
        if expose_relation:
            graph_relations[key] = Relation(
                source_id=source_id,
                relation_type=relation_type,
                target_id=target_id,
                evidence_ids=evidence,
            )

    for path in state.paths:
        if path.status is FindingStatus.REJECTED:
            continue
        for edge in path.edges:
            if edge.relation_type not in COMPOSABLE_LOCAL_RELATIONS:
                continue
            register(
                edge.source_id,
                edge.relation_type,
                edge.target_id,
                edge.evidence_ids,
                path.status,
                path.missing_evidence,
            )

    external_attacker_id = "attacker:external-dependency"
    for relation in state.relations:
        if relation.relation_type not in COMPOSABLE_SECURITY_RELATIONS:
            continue
        if relation.verdict is TriState.FALSE:
            continue
        status = (
            FindingStatus.SUPPORTED
            if relation.verdict is TriState.TRUE
            else FindingStatus.CANDIDATE
        )
        register(
            relation.source_id,
            relation.relation_type,
            relation.target_id,
            relation.evidence_ids,
            status,
            relation.missing_evidence,
        )
        if relation.relation_type in EXTERNAL_DEPENDENCY_RELATIONS:
            register(
                external_attacker_id,
                "may_compromise_mutable_dependency",
                relation.source_id,
                relation.evidence_ids,
                FindingStatus.CANDIDATE,
                ["external dependency compromise"],
                expose_relation=True,
            )

    attacker_id = f"attacker:{profile.value}"
    assessments = {assessment.subject_id: assessment for assessment in state.assessments}

    for workflow in state.workflows:
        if set(workflow.triggers) & {"pull_request", "pull_request_target"}:
            register(
                attacker_id,
                "can_trigger_workflow",
                workflow.workflow_id,
                workflow.evidence_ids,
                expose_relation=True,
            )
        for job in workflow.jobs:
            token_id = f"credential:github-token:{job.job_id}"
            if _credential_write_enabled(job.permissions_effective):
                for step in job.steps:
                    register(
                        step.step_id,
                        "can_access_write_github_token",
                        token_id,
                        sorted(set(job.evidence_ids + step.evidence_ids)),
                        expose_relation=True,
                    )
            for step in job.steps:
                assessment = assessments.get(step.step_id)
                if assessment and assessment.verdict is not TriState.FALSE:
                    register(
                        attacker_id,
                        "controls_repository_execution",
                        step.step_id,
                        assessment.evidence_ids,
                        (
                            FindingStatus.SUPPORTED
                            if assessment.verdict is TriState.TRUE
                            else FindingStatus.CANDIDATE
                        ),
                        assessment.missing_evidence,
                        expose_relation=True,
                    )

    for finding in state.findings:
        if finding.rule_id != "GHA-01-PR-SHELL-INJECTION":
            continue
        matching_path = next(
            (
                path
                for path in state.paths
                if path.path_id == stable_id("path", finding.finding_id) and path.nodes
            ),
            None,
        )
        if matching_path:
            register(
                attacker_id,
                "controls_workflow_input",
                matching_path.nodes[-1],
                finding.evidence_ids,
                finding.status,
                finding.missing_evidence,
                expose_relation=True,
            )

    for identity in state.identities:
        identity_missing = (
            ["cloud identity trust policy"]
            if identity.trust_verified is TriState.UNKNOWN
            else []
        )
        identity_status = (
            FindingStatus.SUPPORTED
            if identity.trust_verified is TriState.TRUE
            else FindingStatus.CANDIDATE
        )
        workflow = next(
            (item for item in state.workflows if item.workflow_id == identity.workflow_id), None
        )
        if workflow is None:
            continue
        job = next((item for item in workflow.jobs if item.job_id == identity.job_id), None)
        if job is None:
            continue
        for step in job.steps:
            register(
                step.step_id,
                "can_request_cloud_identity",
                identity.identity_id,
                sorted(set(identity.evidence_ids + step.evidence_ids)),
                identity_status,
                identity_missing,
                expose_relation=True,
            )

    adjacency: dict[str, list[_EdgeFact]] = {}
    for fact in facts.values():
        adjacency.setdefault(fact.edge.source_id, []).append(fact)
    for edges in adjacency.values():
        edges.sort(key=lambda item: (item.edge.relation_type, item.edge.target_id))

    sinks = {
        fact.edge.target_id
        for fact in facts.values()
        if fact.edge.relation_type
        in {
            "can_access_write_github_token",
            "can_request_cloud_identity",
            *GENERIC_SINK_RELATIONS,
        }
    }
    paths: list[AttackPath] = []

    def walk(
        node_id: str,
        nodes: list[str],
        edges: list[_EdgeFact],
    ) -> None:
        if len(paths) >= MAX_GLOBAL_PATHS or len(edges) >= MAX_GLOBAL_PATH_EDGES:
            return
        if node_id in sinks and edges:
            missing = sorted(
                {item for fact in edges for item in fact.missing_evidence}
            )
            status = (
                FindingStatus.SUPPORTED
                if all(_supported(fact.status) for fact in edges)
                else FindingStatus.CANDIDATE
            )
            attack_edges = [fact.edge.model_copy(deep=True) for fact in edges]
            evidence_ids = sorted(
                {evidence_id for edge in attack_edges for evidence_id in edge.evidence_ids}
            )
            paths.append(
                AttackPath(
                    path_id=stable_id(
                        "path",
                        "global",
                        *nodes,
                        *(edge.relation_type for edge in attack_edges),
                    ),
                    nodes=list(nodes),
                    edges=attack_edges,
                    status=status,
                    evidence_ids=evidence_ids,
                    missing_evidence=missing,
                )
            )
            return
        for fact in adjacency.get(node_id, []):
            target = fact.edge.target_id
            if target in nodes:
                continue
            walk(target, [*nodes, target], [*edges, fact])

    for source_id in (attacker_id, external_attacker_id):
        walk(source_id, [source_id], [])
    unique_paths = {path.path_id: path for path in paths}
    return list(graph_relations.values()), list(unique_paths.values())
