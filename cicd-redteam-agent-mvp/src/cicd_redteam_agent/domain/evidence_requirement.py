from __future__ import annotations

import hashlib

from pydantic import Field

from cicd_redteam_agent.domain.models import (
    AnalysisState,
    EvidenceProvider,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    SchemaModel,
)


class EvidenceFreshness(SchemaModel):
    scope: str = Field(default="run", pattern=r"^(run|resume-revalidate|runtime)$")
    revalidate_on_resume: bool = False


class EvidenceRequirement(SchemaModel):
    requirement_id: str = Field(pattern=r"^req_[0-9a-f]{16}$")
    kind: EvidenceRequirementKind
    provider: EvidenceProvider
    subject_id: str = Field(min_length=1, max_length=300)
    qualifier: str | None = Field(default=None, max_length=300)
    basis_relation_ids: list[str] = Field(default_factory=list, max_length=24)
    frontier_ids: list[str] = Field(default_factory=list, max_length=8)
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    reason: str = Field(min_length=1, max_length=600)
    freshness: EvidenceFreshness = Field(default_factory=EvidenceFreshness)

    def ref(self) -> EvidenceRequirementRef:
        return EvidenceRequirementRef(
            kind=self.kind,
            provider=self.provider,
            subject_id=self.subject_id,
            qualifier=self.qualifier,
        )


def requirement_id_for(
    kind: EvidenceRequirementKind,
    subject_id: str,
    *,
    qualifier: str | None = None,
) -> str:
    digest = hashlib.sha256(
        "|".join((kind.value, subject_id, qualifier or "")).encode()
    ).hexdigest()[:16]
    return f"req_{digest}"


def evidence_key(
    kind: EvidenceRequirementKind,
    subject_id: str,
    *,
    qualifier: str | None = None,
) -> str:
    return "|".join((kind.value, subject_id, qualifier or ""))


def requirement_kind_for_payload_type(payload_type: str) -> EvidenceRequirementKind:
    try:
        return EvidenceRequirementKind(payload_type)
    except ValueError as exc:
        raise ValueError(f"unsupported external evidence payload type: {payload_type}") from exc


def annotate_external_evidence_requirements(state: AnalysisState) -> None:
    """Attach typed resolver requirements without changing human-readable missing evidence."""

    repository_id = state.repository.repository_id
    environment_by_job: dict[str, str] = {}
    for workflow in state.workflows:
        for job in workflow.jobs:
            raw = job.environment
            if isinstance(raw, dict):
                raw = raw.get("name")
            if isinstance(raw, str) and raw.strip() and "${{" not in raw:
                environment_by_job[job.job_id] = raw.strip()
    job_by_context = {
        context.context_id: context.job_id for context in state.job_execution_contexts
    }
    action_relations = {
        "uses_external_action_ref",
        "uses_external_reusable_workflow",
        "external_code_may_execute_in_step",
        "mutable_reusable_workflow_may_execute_in_job",
    }
    for relation in state.relations:
        refs = list(relation.evidence_requirements)
        missing = set(relation.missing_evidence)
        if "repository default GITHUB_TOKEN permissions" in missing:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION,
                    subject_id=repository_id,
                )
            )
        if "fork pull-request write-token repository setting" in missing:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY,
                    subject_id=repository_id,
                )
            )
        if relation.relation_type in action_relations:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
                    subject_id=repository_id,
                )
            )
            action_subject = (
                relation.target_id
                if relation.target_id.startswith("external:")
                else relation.source_id
                if relation.source_id.startswith("external:")
                else None
            )
            if action_subject is not None:
                refs.extend(
                    [
                        EvidenceRequirementRef(
                            kind=EvidenceRequirementKind.GITHUB_ACTION_PROVENANCE,
                            subject_id=action_subject,
                        ),
                        EvidenceRequirementRef(
                            kind=EvidenceRequirementKind.GITHUB_ACTION_ADVISORY,
                            subject_id=action_subject,
                        ),
                    ]
                )
        if relation.relation_type.startswith("secret_"):
            refs.extend(
                [
                    EvidenceRequirementRef(
                        kind=EvidenceRequirementKind.GITHUB_SECRET_METADATA,
                        subject_id=f"{repository_id}:secrets:repository",
                        qualifier="repository",
                    ),
                    EvidenceRequirementRef(
                        kind=EvidenceRequirementKind.GITHUB_SECRET_METADATA,
                        subject_id=f"{repository_id}:secrets:organization",
                        qualifier="organization",
                    ),
                ]
            )
        audit_missing = {
            "runner cleanup",
            "runner ephemerality",
            "historical repository authorization",
            "trusted label mutation provenance",
            "exact label action provenance",
        }
        if relation.relation_type in {
            "runner_process_or_file_state_may_reach_privileged_job",
            "workspace_may_be_reused_by_privileged_job",
        } or missing & audit_missing:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_AUDIT_EVENTS,
                    subject_id=f"{repository_id}:audit",
                )
            )
        job_id = relation.source_id if relation.source_id in environment_by_job else None
        if job_id is None:
            job_id = relation.target_id if relation.target_id in environment_by_job else None
        if job_id is None:
            job_id = job_by_context.get(relation.source_id) or job_by_context.get(
                relation.target_id
            )
        environment = environment_by_job.get(job_id or "")
        if environment is not None and (
            relation.relation_type == "environment_protection_unknown"
            or "environment deployment branch policy" in missing
        ):
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
                    subject_id=f"{repository_id}:environment:{environment}",
                    qualifier=environment,
                )
            )
        if environment is not None and "environment deployment ref eligibility" in missing:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
                    subject_id=f"{repository_id}:environment:{environment}",
                    qualifier=environment,
                )
            )
        unique = {
            (ref.kind, ref.provider, ref.subject_id, ref.qualifier): ref for ref in refs
        }
        relation.evidence_requirements = sorted(
            unique.values(),
            key=lambda item: (item.kind.value, item.subject_id, item.qualifier or ""),
        )
