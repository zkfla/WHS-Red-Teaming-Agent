from __future__ import annotations

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackEdge,
    AttackerProfile,
    AttackPath,
    Confidence,
    ControlAssessment,
    Finding,
    FindingStatus,
    TriState,
)


def _permission_is_privileged(value: dict[str, str] | str | None) -> bool:
    if isinstance(value, str):
        return value == "write-all"
    if not isinstance(value, dict):
        return False
    return any(str(permission).lower() == "write" for permission in value.values())


def assess_attacker_control(
    state: AnalysisState, profile: AttackerProfile
) -> list[ControlAssessment]:
    assessments: list[ControlAssessment] = []
    relation_sources = {
        relation.source_id: relation
        for relation in state.relations
        if relation.relation_type == "executes_repository_file"
    }
    for workflow in state.workflows:
        trigger_names = set(workflow.triggers)
        pr_trigger = bool(trigger_names & {"pull_request", "pull_request_target"})
        for job in workflow.jobs:
            for step in job.steps:
                if step.step_id not in relation_sources:
                    continue
                evidence_ids = sorted(
                    set(workflow.evidence_ids + job.evidence_ids + step.evidence_ids)
                )
                if profile is AttackerProfile.REPO_WRITER:
                    verdict = TriState.TRUE
                    reason = (
                        "A repository writer can modify repository-controlled executable files."
                    )
                    missing: list[str] = []
                elif not pr_trigger:
                    verdict = TriState.FALSE
                    reason = "No fork-PR-capable trigger was found for this workflow."
                    missing = []
                elif "pull_request_target" in trigger_names:
                    checkout = " ".join(
                        f"{candidate.uses} {candidate.with_}"
                        for candidate in job.steps
                        if candidate.uses and candidate.uses.startswith("actions/checkout@")
                    )
                    if "pull_request.head" in checkout or "head.sha" in checkout:
                        verdict = TriState.TRUE
                        reason = (
                            "pull_request_target explicitly checks out "
                            "pull-request-controlled content."
                        )
                        missing = []
                    else:
                        verdict = TriState.UNKNOWN
                        reason = (
                            "pull_request_target normally uses base content; "
                            "checkout provenance is unresolved."
                        )
                        missing = ["runtime checkout revision", "repository settings"]
                else:
                    verdict = TriState.TRUE
                    reason = (
                        "pull_request execution can run a repository-controlled "
                        "file from PR content."
                    )
                    missing = []
                assessments.append(
                    ControlAssessment(
                        subject_id=step.step_id,
                        attacker_profile=profile,
                        verdict=verdict,
                        evidence_ids=evidence_ids,
                        missing_evidence=missing,
                        reason=reason,
                    )
                )
    return assessments


def derive_attack_paths(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    assessments = {assessment.subject_id: assessment for assessment in state.assessments}
    secret_steps = {
        reference.step_id for reference in state.secret_references if reference.step_id is not None
    }
    secret_jobs = {
        reference.job_id for reference in state.secret_references if reference.step_id is None
    }
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for workflow in state.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                privileged = (
                    _permission_is_privileged(job.permissions_effective)
                    or job.job_id in secret_jobs
                    or step.step_id in secret_steps
                )
                assessment = assessments.get(step.step_id)
                if not assessment or assessment.verdict is TriState.FALSE:
                    continue
                oidc = isinstance(job.permissions_effective, dict) and (
                    job.permissions_effective.get("id-token") == "write"
                )
                if not privileged and not oidc:
                    continue
                supported = assessment.verdict is TriState.TRUE
                status = FindingStatus.SUPPORTED if supported else FindingStatus.CANDIDATE
                evidence_ids = sorted(set(assessment.evidence_ids + job.evidence_ids))
                finding_id = stable_id("finding", workflow.workflow_id, job.job_id, step.step_id)
                missing = [] if supported else assessment.missing_evidence
                findings.append(
                    Finding(
                        finding_id=finding_id,
                        rule_id="CICD-UNTRUSTED-PRIVILEGED-EXECUTION",
                        title="Repository-controlled code may execute in a privileged job",
                        status=status,
                        evidence_ids=evidence_ids,
                        missing_evidence=missing,
                        confidence=Confidence.HIGH if supported else Confidence.LOW,
                        summary=(
                            "Local evidence links an untrusted change path, repository-controlled "
                            "execution, and a privileged job. External controls remain unverified."
                        ),
                    )
                )
                edge = AttackEdge(
                    source_id=workflow.workflow_id,
                    relation_type="contains_job",
                    target_id=job.job_id,
                    evidence_ids=workflow.evidence_ids + job.evidence_ids,
                )
                execution = next(
                    (
                        relation
                        for relation in state.relations
                        if relation.source_id == step.step_id
                        and relation.relation_type == "executes_repository_file"
                    ),
                    None,
                )
                edges = [edge]
                nodes = [workflow.workflow_id, job.job_id, step.step_id]
                edges.append(
                    AttackEdge(
                        source_id=job.job_id,
                        relation_type="contains_step",
                        target_id=step.step_id,
                        evidence_ids=job.evidence_ids + step.evidence_ids,
                    )
                )
                if execution:
                    edges.append(
                        AttackEdge(
                            source_id=execution.source_id,
                            relation_type=execution.relation_type,
                            target_id=execution.target_id,
                            evidence_ids=execution.evidence_ids,
                        )
                    )
                    nodes.append(execution.target_id)
                    evidence_ids = sorted(set(evidence_ids + execution.evidence_ids))
                paths.append(
                    AttackPath(
                        path_id=stable_id("path", finding_id),
                        nodes=nodes,
                        edges=edges,
                        status=status,
                        evidence_ids=evidence_ids,
                        missing_evidence=missing,
                    )
                )
    return findings, paths
