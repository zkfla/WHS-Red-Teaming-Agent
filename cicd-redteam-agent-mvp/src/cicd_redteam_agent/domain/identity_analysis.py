from __future__ import annotations

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    CloudIdentityCandidate,
    Confidence,
    TriState,
)


def derive_identity_candidates(state: AnalysisState) -> list[CloudIdentityCandidate]:
    assessments = {assessment.subject_id: assessment for assessment in state.assessments}
    secret_by_job: dict[str, list[str]] = {}
    for reference in state.secret_references:
        secret_by_job.setdefault(reference.job_id, []).append(reference.reference_id)
    identities: list[CloudIdentityCandidate] = []
    for workflow in state.workflows:
        for job in workflow.jobs:
            oidc = isinstance(job.permissions_effective, dict) and (
                job.permissions_effective.get("id-token") == "write"
            )
            for step in job.steps:
                if not step.uses or not step.uses.startswith(
                    "aws-actions/configure-aws-credentials@"
                ):
                    continue
                role_arn = step.with_.get("role-to-assume")
                related = [a for a in assessments.values() if a.subject_id == step.step_id]
                if not related:
                    related = [
                        assessment
                        for assessment in assessments.values()
                        if assessment.subject_id.startswith(job.job_id)
                    ]
                control = TriState.UNKNOWN
                if any(item.verdict is TriState.TRUE for item in related):
                    control = TriState.TRUE
                elif related and all(item.verdict is TriState.FALSE for item in related):
                    control = TriState.FALSE
                evidence_ids = sorted(
                    set(
                        job.evidence_ids
                        + step.evidence_ids
                        + [x for a in related for x in a.evidence_ids]
                    )
                )
                identities.append(
                    CloudIdentityCandidate(
                        identity_id=stable_id(
                            "identity", workflow.workflow_id, job.job_id, role_arn
                        ),
                        provider="aws",
                        identity_type="iam-role" if role_arn else "unknown-aws-identity",
                        mechanism="github-oidc" if oidc else "static-or-unknown",
                        repository_id=state.repository.repository_id,
                        workflow_id=workflow.workflow_id,
                        job_id=job.job_id,
                        identity_reference=str(role_arn) if role_arn else None,
                        role_arn=str(role_arn) if role_arn else None,
                        secret_reference_ids=secret_by_job.get(job.job_id, []),
                        oidc_enabled=TriState.TRUE if oidc else TriState.UNKNOWN,
                        attacker_control=control,
                        trust_verified=TriState.UNKNOWN,
                        evidence_ids=evidence_ids,
                        confidence=Confidence.MEDIUM if oidc and role_arn else Confidence.LOW,
                        summary=(
                            "Local workflow evidence identifies an AWS credential path. "
                            "The IAM trust policy and AssumeRole success were not verified."
                        ),
                    )
                )
    return identities
