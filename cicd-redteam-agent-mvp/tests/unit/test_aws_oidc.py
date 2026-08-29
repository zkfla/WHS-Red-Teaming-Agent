from __future__ import annotations

from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.semantic_snapshot import rebuild_semantic_snapshot
from cicd_redteam_agent.domain.evidence_requirement import evidence_key
from cicd_redteam_agent.domain.investigation import (
    AwsIamRoleTrustPolicyEvidence,
    AwsOidcProviderEvidence,
    GitHubOidcSubjectConfigEvidence,
    KnowledgeState,
    RepositoryMetadataEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    EvidenceRequirementKind,
    OidcAuthenticationMode,
    TriState,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURE = Path(__file__).parents[1] / "fixtures" / "oidc-risk"
ROLE_ARN = "arn:aws:iam::123456789012:role/ci-deploy"
PROVIDER_ARN = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"


def _knowledge(
    repository_id: str,
    *,
    effect: str = "Allow",
    subject: str = "repo:example/repo:pull_request",
):
    values = {
        evidence_key(EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA, repository_id): (
            RepositoryMetadataEvidence(
                owner="example",
                repository="repo",
                private=False,
                visibility="public",
                default_branch="main",
                owner_id=11,
                repository_id_number=22,
            )
        ),
        evidence_key(EvidenceRequirementKind.GITHUB_OIDC_SUBJECT_CONFIG, repository_id): (
            GitHubOidcSubjectConfigEvidence(
                use_default=True,
                use_immutable_subject=False,
            )
        ),
        evidence_key(
            EvidenceRequirementKind.AWS_IAM_ROLE_TRUST_POLICY,
            ROLE_ARN,
            qualifier=ROLE_ARN,
        ): AwsIamRoleTrustPolicyEvidence(
            role_arn=ROLE_ARN,
            role_name="ci-deploy",
            assume_role_policy={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": f"{effect}GitHub",
                        "Effect": effect,
                        "Principal": {"Federated": PROVIDER_ARN},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {
                                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                "token.actions.githubusercontent.com:sub": subject,
                            }
                        },
                    }
                ],
            },
        ),
        evidence_key(
            EvidenceRequirementKind.AWS_IAM_OIDC_PROVIDER,
            PROVIDER_ARN,
            qualifier=PROVIDER_ARN,
        ): AwsOidcProviderEvidence(
            provider_arn=PROVIDER_ARN,
            url="token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        ),
    }
    return KnowledgeState(external_evidence=values)


@pytest.mark.asyncio
async def test_complete_matching_trust_recomputes_relation_and_identity_true(tmp_path: Path):
    report, _ = await analyze_repository(FIXTURE, AnalysisConfig(output_dir=tmp_path))
    baseline = report.state
    assert baseline.aws_oidc_trust_assessments[0].verdict is TriState.UNKNOWN

    rebuilt = rebuild_semantic_snapshot(
        baseline,
        RepositoryBoundary(FIXTURE),
        AttackerProfile.REPO_WRITER,
        knowledge_state=_knowledge(baseline.repository.repository_id),
    )

    assessment = rebuilt.aws_oidc_trust_assessments[0]
    assert assessment.authentication_mode is OidcAuthenticationMode.GITHUB_OIDC
    assert assessment.verdict is TriState.TRUE
    assert assessment.actual_assume_succeeded is TriState.UNKNOWN
    assert assessment.claim_candidates[0].subject == "repo:example/repo:pull_request"
    relation = next(
        item for item in rebuilt.relations if item.relation_type == "oidc_may_assume_cloud_identity"
    )
    assert relation.verdict is TriState.TRUE
    assert rebuilt.identities[0].trust_verified is TriState.TRUE
    assert rebuilt.identities[0].actual_assume_succeeded is TriState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "subject"),
    [
        ("Allow", "repo:other/repo:pull_request"),
        ("Deny", "repo:example/repo:pull_request"),
    ],
)
async def test_complete_nonmatching_or_denied_trust_is_false(
    tmp_path: Path, effect: str, subject: str
):
    report, _ = await analyze_repository(FIXTURE, AnalysisConfig(output_dir=tmp_path))
    knowledge = _knowledge(
        report.state.repository.repository_id,
        effect=effect,
        subject=subject,
    )
    rebuilt = rebuild_semantic_snapshot(
        report.state,
        RepositoryBoundary(FIXTURE),
        AttackerProfile.REPO_WRITER,
        knowledge_state=knowledge,
    )
    assert rebuilt.aws_oidc_trust_assessments[0].verdict is TriState.FALSE


@pytest.mark.asyncio
async def test_custom_subject_template_stays_unknown(tmp_path: Path):
    report, _ = await analyze_repository(FIXTURE, AnalysisConfig(output_dir=tmp_path))
    knowledge = _knowledge(report.state.repository.repository_id)
    key = evidence_key(
        EvidenceRequirementKind.GITHUB_OIDC_SUBJECT_CONFIG,
        report.state.repository.repository_id,
    )
    knowledge.external_evidence[key] = GitHubOidcSubjectConfigEvidence(
        use_default=False,
        include_claim_keys=["repository_id"],
    )
    rebuilt = rebuild_semantic_snapshot(
        report.state,
        RepositoryBoundary(FIXTURE),
        AttackerProfile.REPO_WRITER,
        knowledge_state=knowledge,
    )
    assert rebuilt.aws_oidc_trust_assessments[0].verdict is TriState.UNKNOWN
    assert "custom GitHub OIDC subject template interpretation" in (
        rebuilt.aws_oidc_trust_assessments[0].missing_evidence
    )
