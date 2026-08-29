from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from cicd_redteam_agent.domain.aws_oidc import GITHUB_PROVIDER_URL, ROLE_ARN
from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement
from cicd_redteam_agent.domain.investigation import (
    AwsIamRoleTrustPolicyEvidence,
    AwsOidcProviderEvidence,
    ObservationSource,
    RedTeamSession,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, EvidenceRequirementKind
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverDescriptor,
)


class IamClient(Protocol):
    def get_role(self, *, RoleName: str) -> dict[str, Any]: ...

    def get_open_id_connect_provider(self, *, OpenIDConnectProviderArn: str) -> dict[str, Any]: ...


def _failure(exc: Exception) -> EvidenceResolutionError:
    if isinstance(exc, NoCredentialsError):
        return EvidenceResolutionError(
            ResolutionFailureKind.AUTH_REQUIRED, "AWS credentials are unavailable"
        )
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            return EvidenceResolutionError(
                ResolutionFailureKind.FORBIDDEN, "AWS IAM read access was denied"
            )
        if code in {"NoSuchEntity", "NoSuchEntityException"}:
            return EvidenceResolutionError(
                ResolutionFailureKind.AMBIGUOUS_NOT_FOUND,
                "AWS IAM resource was not found or is not visible",
            )
    return EvidenceResolutionError(
        ResolutionFailureKind.TRANSIENT, f"AWS IAM observation failed: {type(exc).__name__}"
    )


@dataclass(frozen=True)
class AwsIamResolver:
    client: IamClient = field(repr=False)

    observation_source = ObservationSource.AWS
    descriptor = ResolverDescriptor(
        resolver_id="aws.iam-oidc-trust.v1",
        provider=EvidenceProvider.AWS,
        supported_kinds=frozenset(
            {
                EvidenceRequirementKind.AWS_IAM_ROLE_TRUST_POLICY,
                EvidenceRequirementKind.AWS_IAM_OIDC_PROVIDER,
            }
        ),
        required_permissions=("iam:GetRole", "iam:GetOpenIDConnectProvider"),
        max_calls=1,
        timeout_seconds=15.0,
    )

    @classmethod
    def from_profile(cls, profile_name: str | None = None) -> AwsIamResolver:
        session = boto3.Session(profile_name=profile_name)
        return cls(client=session.client("iam"))

    async def resolve(self, session: RedTeamSession, requirement: EvidenceRequirement):
        del session
        try:
            if requirement.kind is EvidenceRequirementKind.AWS_IAM_ROLE_TRUST_POLICY:
                return await self._role(requirement)
            if requirement.kind is EvidenceRequirementKind.AWS_IAM_OIDC_PROVIDER:
                return await self._provider(requirement)
        except (BotoCoreError, ClientError) as exc:
            raise _failure(exc) from None
        raise EvidenceResolutionError(
            ResolutionFailureKind.UNSUPPORTED,
            f"AWS resolver does not support {requirement.kind.value}",
        )

    async def _role(self, requirement: EvidenceRequirement) -> AwsIamRoleTrustPolicyEvidence:
        role_arn = requirement.qualifier or requirement.subject_id
        match = ROLE_ARN.fullmatch(role_arn)
        if match is None:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE, "evidence subject is not an IAM role ARN"
            )
        role_name = match.group("name").rsplit("/", 1)[-1]
        response = await asyncio.to_thread(self.client.get_role, RoleName=role_name)
        role = response.get("Role")
        if not isinstance(role, dict) or role.get("Arn") != role_arn:
            raise EvidenceResolutionError(
                ResolutionFailureKind.AMBIGUOUS_NOT_FOUND,
                "AWS GetRole response did not match the requested role ARN",
            )
        policy: object = role.get("AssumeRolePolicyDocument")
        if isinstance(policy, str):
            try:
                policy = json.loads(unquote(policy))
            except (ValueError, TypeError):
                policy = None
        if not isinstance(policy, dict):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT, "AWS role trust policy was not a JSON object"
            )
        return AwsIamRoleTrustPolicyEvidence(
            role_arn=role_arn,
            role_name=role_name,
            role_id=str(role.get("RoleId")) if role.get("RoleId") else None,
            assume_role_policy=policy,
        )

    async def _provider(self, requirement: EvidenceRequirement) -> AwsOidcProviderEvidence:
        provider_arn = requirement.qualifier or requirement.subject_id
        response = await asyncio.to_thread(
            self.client.get_open_id_connect_provider,
            OpenIDConnectProviderArn=provider_arn,
        )
        url = response.get("Url")
        client_ids = response.get("ClientIDList")
        if (
            url != GITHUB_PROVIDER_URL
            or not isinstance(client_ids, list)
            or not all(isinstance(item, str) for item in client_ids)
        ):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "AWS OIDC provider response did not identify the GitHub issuer",
            )
        return AwsOidcProviderEvidence(
            provider_arn=provider_arn,
            url=url,
            client_ids=client_ids,
        )
