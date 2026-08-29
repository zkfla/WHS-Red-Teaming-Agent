from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from typing import Any

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    AwsIamRoleTrustPolicyEvidence,
    AwsOidcProviderEvidence,
    GitHubOidcSubjectConfigEvidence,
    RepositoryMetadataEvidence,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AwsOidcTrustAssessment,
    EvidenceProvider,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    Job,
    OidcAuthenticationMode,
    OidcClaimCandidate,
    PermissionLevel,
    TriState,
    Workflow,
)

ACTION_PREFIX = "aws-actions/configure-aws-credentials@"
ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):iam::(?P<account>[0-9]{12}):role/(?P<name>[^\s]+)$"
)
GITHUB_PROVIDER_URL = "token.actions.githubusercontent.com"


def _literal(value: object) -> str | None:
    if not isinstance(value, (str, int, bool)):
        return None
    text = str(value).strip()
    return text if text and "${{" not in text else None


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    literal = _literal(value)
    if literal is None:
        return None
    if literal.lower() == "true":
        return True
    if literal.lower() == "false":
        return False
    return None


def _permission(state: AnalysisState, job_id: str) -> TriState:
    contexts = [item for item in state.job_execution_contexts if item.job_id == job_id]
    levels = [item.effective_permissions.get("id-token") for item in contexts]
    if levels and all(level is PermissionLevel.WRITE for level in levels):
        return TriState.TRUE
    if any(level in {PermissionLevel.WRITE, PermissionLevel.UNKNOWN} for level in levels):
        return TriState.UNKNOWN
    return TriState.FALSE


def classify_authentication_mode(
    state: AnalysisState, job: Job, inputs: dict[str, Any]
) -> OidcAuthenticationMode:
    role = _literal(inputs.get("role-to-assume"))
    if inputs.get("role-to-assume") is not None and role is None:
        return OidcAuthenticationMode.UNKNOWN
    for key, mode in (
        ("aws-access-key-id", OidcAuthenticationMode.STATIC_CREDENTIALS),
        ("web-identity-token-file", OidcAuthenticationMode.WEB_IDENTITY_TOKEN_FILE),
    ):
        if inputs.get(key) is not None:
            return mode if _literal(inputs.get(key)) is not None else OidcAuthenticationMode.UNKNOWN
    for key, mode in (
        ("role-chaining", OidcAuthenticationMode.ROLE_CHAINING),
        ("force-skip-oidc", OidcAuthenticationMode.SKIPPED),
    ):
        if inputs.get(key) is not None:
            flag = _boolean(inputs.get(key))
            if flag is None:
                return OidcAuthenticationMode.UNKNOWN
            if flag:
                return mode
    if role is None:
        return OidcAuthenticationMode.SKIPPED
    permission = _permission(state, job.job_id)
    if permission is TriState.TRUE:
        return OidcAuthenticationMode.GITHUB_OIDC
    if permission is TriState.FALSE:
        return OidcAuthenticationMode.SKIPPED
    return OidcAuthenticationMode.UNKNOWN


def _escape_subject(value: str) -> str:
    return value.replace(":", "%3A")


def _single_filter(config: object, key: str) -> str | None:
    if not isinstance(config, dict):
        return None
    value = config.get(key)
    if isinstance(value, str):
        return _literal(value)
    if isinstance(value, list) and len(value) == 1:
        return _literal(value[0])
    return None


def _subject_suffix(workflow: Workflow, job: Job, event_name: str) -> tuple[str | None, list[str]]:
    environment: object = job.environment
    if isinstance(environment, dict):
        environment = environment.get("name")
    environment_name = _literal(environment)
    if environment is not None and environment_name is None:
        return None, ["concrete GitHub environment name"]
    if environment_name is not None:
        return f"environment:{_escape_subject(environment_name)}", []
    if event_name == "pull_request":
        return "pull_request", []
    if event_name == "push":
        trigger = workflow.triggers.get("push")
        branch = _single_filter(trigger, "branches")
        tag = _single_filter(trigger, "tags")
        if branch and not tag:
            return f"ref:refs/heads/{branch}", []
        if tag and not branch:
            return f"ref:refs/tags/{tag}", []
        return None, ["single concrete push branch or tag"]
    return None, [f"GitHub OIDC subject mapping for {event_name}"]


def build_claim_candidates(
    state: AnalysisState,
    workflow: Workflow,
    job: Job,
    *,
    audience: str,
    metadata: RepositoryMetadataEvidence | None,
    subject_config: GitHubOidcSubjectConfigEvidence | None,
) -> list[OidcClaimCandidate]:
    candidates: list[OidcClaimCandidate] = []
    contexts = [item for item in state.trigger_contexts if item.workflow_id == workflow.workflow_id]
    for context in contexts:
        group_id = context.context_id
        suffix, suffix_missing = _subject_suffix(workflow, job, context.event_name)
        if metadata is None:
            candidates.append(
                OidcClaimCandidate(
                    trigger_context_id=context.context_id,
                    variant_group_id=group_id,
                    audience=audience,
                    subject_format="unknown",
                    complete=TriState.UNKNOWN,
                    missing_evidence=["GitHub repository numeric identity and name"],
                )
            )
            continue
        if subject_config is not None and not subject_config.use_default:
            candidates.append(
                OidcClaimCandidate(
                    trigger_context_id=context.context_id,
                    variant_group_id=group_id,
                    audience=audience,
                    subject_format="custom",
                    complete=TriState.UNKNOWN,
                    missing_evidence=["custom GitHub OIDC subject template interpretation"],
                )
            )
            continue
        if suffix is None:
            candidates.append(
                OidcClaimCandidate(
                    trigger_context_id=context.context_id,
                    variant_group_id=group_id,
                    audience=audience,
                    subject_format="unknown",
                    complete=TriState.UNKNOWN,
                    missing_evidence=suffix_missing,
                )
            )
            continue
        formats: list[str]
        if subject_config is None or subject_config.use_immutable_subject is None:
            formats = ["legacy", "immutable"]
        else:
            formats = ["immutable" if subject_config.use_immutable_subject else "legacy"]
        for subject_format in formats:
            if subject_format == "immutable":
                if not metadata.owner_id or not metadata.repository_id_number:
                    candidates.append(
                        OidcClaimCandidate(
                            trigger_context_id=context.context_id,
                            variant_group_id=group_id,
                            audience=audience,
                            subject_format="immutable",
                            complete=TriState.UNKNOWN,
                            missing_evidence=["GitHub repository and owner numeric IDs"],
                        )
                    )
                    continue
                prefix = (
                    f"repo:{metadata.owner}@{metadata.owner_id}/"
                    f"{metadata.repository}@{metadata.repository_id_number}"
                )
            else:
                prefix = f"repo:{metadata.owner}/{metadata.repository}"
            candidates.append(
                OidcClaimCandidate(
                    trigger_context_id=context.context_id,
                    variant_group_id=group_id,
                    audience=audience,
                    subject=f"{prefix}:{suffix}",
                    subject_format=subject_format,  # type: ignore[arg-type]
                    complete=TriState.TRUE,
                )
            )
    return candidates


def _values(value: object) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _matches(value: str, expected: list[str], *, like: bool) -> bool:
    return any(fnmatch.fnmatchcase(value, item) if like else value == item for item in expected)


def _statement_verdict(
    statement: dict[str, object], *, provider_arn: str, audience: str, subject: str
) -> tuple[TriState, str | None, bool]:
    if "NotPrincipal" in statement or "NotAction" in statement:
        return TriState.UNKNOWN, None, False
    principal = statement.get("Principal")
    federated: object = None
    if isinstance(principal, dict):
        federated = principal.get("Federated")
    principal_values = _values(federated)
    if principal_values is None:
        return (
            (TriState.FALSE, None, False) if federated is None else (TriState.UNKNOWN, None, False)
        )
    if provider_arn not in principal_values:
        return TriState.FALSE, None, False
    actions = _values(statement.get("Action"))
    if actions is None:
        return TriState.UNKNOWN, None, False
    if not any(fnmatch.fnmatchcase("sts:AssumeRoleWithWebIdentity", action) for action in actions):
        return TriState.FALSE, None, False
    condition = statement.get("Condition", {})
    if not isinstance(condition, dict):
        return TriState.UNKNOWN, None, False
    claims = {
        f"{GITHUB_PROVIDER_URL}:aud": audience,
        f"{GITHUB_PROVIDER_URL}:sub": subject,
    }
    for operator, raw_conditions in condition.items():
        if operator not in {"StringEquals", "StringLike"} or not isinstance(raw_conditions, dict):
            return TriState.UNKNOWN, None, False
        for key, expected in raw_conditions.items():
            expected_values = _values(expected)
            if key not in claims or expected_values is None:
                return TriState.UNKNOWN, None, False
            if not _matches(claims[key], expected_values, like=operator == "StringLike"):
                return TriState.FALSE, None, False
    effect = statement.get("Effect")
    if effect not in {"Allow", "Deny"}:
        return TriState.UNKNOWN, None, False
    sid = statement.get("Sid")
    return TriState.TRUE, str(sid) if isinstance(sid, str) else None, effect == "Deny"


def evaluate_trust(
    candidates: list[OidcClaimCandidate],
    *,
    role_trust: AwsIamRoleTrustPolicyEvidence,
    provider: AwsOidcProviderEvidence,
) -> tuple[TriState, list[str]]:
    document = role_trust.assume_role_policy
    raw_statements = document.get("Statement")
    statements = raw_statements if isinstance(raw_statements, list) else [raw_statements]
    if not statements or not all(isinstance(item, dict) for item in statements):
        return TriState.UNKNOWN, []
    by_group: dict[str, list[TriState]] = {}
    matched_sids: set[str] = set()
    for candidate in candidates:
        if candidate.complete is not TriState.TRUE or candidate.subject is None:
            by_group.setdefault(candidate.variant_group_id, []).append(TriState.UNKNOWN)
            continue
        if candidate.audience not in provider.client_ids:
            by_group.setdefault(candidate.variant_group_id, []).append(TriState.FALSE)
            continue
        allow = False
        deny = False
        unknown = False
        for statement in statements:
            verdict, sid, is_deny = _statement_verdict(
                statement,
                provider_arn=provider.provider_arn,
                audience=candidate.audience,
                subject=candidate.subject,
            )
            if verdict is TriState.UNKNOWN:
                unknown = True
            elif verdict is TriState.TRUE:
                matched_sids.update([sid] if sid else [])
                deny = deny or is_deny
                allow = allow or not is_deny
        candidate_verdict = (
            TriState.FALSE
            if deny
            else TriState.TRUE
            if allow and not unknown
            else TriState.UNKNOWN
            if unknown
            else TriState.FALSE
        )
        by_group.setdefault(candidate.variant_group_id, []).append(candidate_verdict)
    group_verdicts: list[TriState] = []
    for verdicts in by_group.values():
        if verdicts and all(item is TriState.TRUE for item in verdicts):
            group_verdicts.append(TriState.TRUE)
        elif verdicts and all(item is TriState.FALSE for item in verdicts):
            group_verdicts.append(TriState.FALSE)
        else:
            group_verdicts.append(TriState.UNKNOWN)
    if any(item is TriState.TRUE for item in group_verdicts):
        return TriState.TRUE, sorted(matched_sids)
    if group_verdicts and all(item is TriState.FALSE for item in group_verdicts):
        return TriState.FALSE, sorted(matched_sids)
    return TriState.UNKNOWN, sorted(matched_sids)


def _find(items: Iterable[object], item_type: type[Any]) -> Any | None:
    return next((item for item in items if isinstance(item, item_type)), None)


def derive_aws_oidc_trust_assessments(
    state: AnalysisState,
    *,
    external_evidence: Iterable[object] = (),
) -> list[AwsOidcTrustAssessment]:
    evidence = list(external_evidence)
    metadata = _find(evidence, RepositoryMetadataEvidence)
    subject_config = _find(evidence, GitHubOidcSubjectConfigEvidence)
    trusts = {
        item.role_arn: item for item in evidence if isinstance(item, AwsIamRoleTrustPolicyEvidence)
    }
    providers = {
        item.provider_arn: item for item in evidence if isinstance(item, AwsOidcProviderEvidence)
    }
    results: list[AwsOidcTrustAssessment] = []
    repository_id = state.repository.repository_id
    for workflow in state.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                if not (step.uses or "").startswith(ACTION_PREFIX):
                    continue
                role_arn = _literal(step.with_.get("role-to-assume"))
                mode = classify_authentication_mode(state, job, step.with_)
                missing: list[str] = []
                refs: list[EvidenceRequirementRef] = []
                audience = _literal(step.with_.get("audience") or "sts.amazonaws.com")
                role_match = ROLE_ARN.fullmatch(role_arn or "")
                provider_arn = (
                    f"arn:{role_match.group('partition')}:iam::{role_match.group('account')}:"
                    f"oidc-provider/{GITHUB_PROVIDER_URL}"
                    if role_match
                    else None
                )
                candidates: list[OidcClaimCandidate] = []
                verdict = (
                    TriState.FALSE
                    if mode is not OidcAuthenticationMode.UNKNOWN
                    else TriState.UNKNOWN
                )
                matched: list[str] = []
                if mode in {
                    OidcAuthenticationMode.GITHUB_OIDC,
                    OidcAuthenticationMode.UNKNOWN,
                }:
                    verdict = TriState.UNKNOWN
                    if mode is OidcAuthenticationMode.UNKNOWN:
                        missing.append("configure-aws-credentials authentication mode")
                    if audience is None:
                        missing.append("concrete AWS OIDC audience")
                    if role_match is None:
                        missing.append("concrete valid AWS IAM role ARN")
                    refs.extend(
                        [
                            EvidenceRequirementRef(
                                kind=EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
                                subject_id=repository_id,
                            ),
                            EvidenceRequirementRef(
                                kind=EvidenceRequirementKind.GITHUB_OIDC_SUBJECT_CONFIG,
                                subject_id=repository_id,
                            ),
                        ]
                    )
                    if metadata is None:
                        missing.append("GitHub repository metadata")
                    if subject_config is None:
                        missing.append("GitHub OIDC subject configuration")
                    if role_arn and role_match:
                        refs.append(
                            EvidenceRequirementRef(
                                kind=EvidenceRequirementKind.AWS_IAM_ROLE_TRUST_POLICY,
                                provider=EvidenceProvider.AWS,
                                subject_id=role_arn,
                                qualifier=role_arn,
                            )
                        )
                    if provider_arn:
                        refs.append(
                            EvidenceRequirementRef(
                                kind=EvidenceRequirementKind.AWS_IAM_OIDC_PROVIDER,
                                provider=EvidenceProvider.AWS,
                                subject_id=provider_arn,
                                qualifier=provider_arn,
                            )
                        )
                    if audience and role_match:
                        candidates = build_claim_candidates(
                            state,
                            workflow,
                            job,
                            audience=audience,
                            metadata=metadata,
                            subject_config=subject_config,
                        )
                        trust = trusts.get(role_arn or "")
                        provider = providers.get(provider_arn or "")
                        if trust is None:
                            missing.append("AWS IAM role trust policy")
                        if provider is None:
                            missing.append("AWS IAM OIDC provider client IDs")
                        if trust is not None and provider is not None:
                            trust_verdict, matched = evaluate_trust(
                                candidates, role_trust=trust, provider=provider
                            )
                            if mode is OidcAuthenticationMode.GITHUB_OIDC:
                                verdict = trust_verdict
                            missing.extend(
                                item
                                for candidate in candidates
                                for item in candidate.missing_evidence
                            )
                results.append(
                    AwsOidcTrustAssessment(
                        assessment_id=stable_id(
                            "aws-oidc-trust", workflow.workflow_id, job.job_id, step.step_id
                        ),
                        workflow_id=workflow.workflow_id,
                        job_id=job.job_id,
                        step_id=step.step_id,
                        role_arn=role_arn,
                        provider_arn=provider_arn,
                        authentication_mode=mode,
                        verdict=verdict,
                        audience=audience,
                        claim_candidates=candidates,
                        matched_statement_ids=matched,
                        missing_evidence=sorted(set(missing)),
                        evidence_requirements=refs,
                    )
                )
    return results
