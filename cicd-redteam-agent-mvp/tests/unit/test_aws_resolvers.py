from __future__ import annotations

import pytest

from cicd_redteam_agent.domain.evidence_requirement import (
    EvidenceRequirement,
    requirement_id_for,
)
from cicd_redteam_agent.domain.investigation import (
    AwsIamRoleTrustPolicyEvidence,
    AwsOidcProviderEvidence,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, EvidenceRequirementKind
from cicd_redteam_agent.framework.aws_resolvers import AwsIamResolver

ROLE_ARN = "arn:aws:iam::123456789012:role/path/ci-deploy"
PROVIDER_ARN = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"


class FakeIamClient:
    def get_role(self, *, RoleName: str):
        assert RoleName == "ci-deploy"
        return {
            "Role": {
                "Arn": ROLE_ARN,
                "RoleId": "AROATEST",
                "AssumeRolePolicyDocument": {
                    "Statement": [{"Effect": "Allow"}],
                },
            }
        }

    def get_open_id_connect_provider(self, *, OpenIDConnectProviderArn: str):
        assert OpenIDConnectProviderArn == PROVIDER_ARN
        return {
            "Url": "token.actions.githubusercontent.com",
            "ClientIDList": ["sts.amazonaws.com"],
        }


def _requirement(kind: EvidenceRequirementKind, subject: str) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id=requirement_id_for(kind, subject, qualifier=subject),
        kind=kind,
        provider=EvidenceProvider.AWS,
        subject_id=subject,
        qualifier=subject,
        reason="test read-only AWS IAM evidence",
    )


@pytest.mark.asyncio
async def test_aws_resolver_reads_exact_role_and_provider_without_credentials_in_payload():
    resolver = AwsIamResolver(FakeIamClient())
    role = await resolver.resolve(
        None,  # type: ignore[arg-type]
        _requirement(EvidenceRequirementKind.AWS_IAM_ROLE_TRUST_POLICY, ROLE_ARN),
    )
    provider = await resolver.resolve(
        None,  # type: ignore[arg-type]
        _requirement(EvidenceRequirementKind.AWS_IAM_OIDC_PROVIDER, PROVIDER_ARN),
    )
    assert isinstance(role, AwsIamRoleTrustPolicyEvidence)
    assert role.role_name == "ci-deploy"
    assert isinstance(provider, AwsOidcProviderEvidence)
    assert provider.client_ids == ["sts.amazonaws.com"]
    assert "credential" not in role.model_dump_json().lower()
