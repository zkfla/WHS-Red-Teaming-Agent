from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement, requirement_id_for
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    InvestigationTask,
    InvestigationTaskKind,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    EvidenceProvider,
    EvidenceRequirementKind,
    ExecutionResult,
    TriState,
)
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.github_observer import (
    GitHubHttpResponse,
    UrllibGitHubJsonTransport,
)
from cicd_redteam_agent.framework.github_resolvers import GitHubCompositeResolver
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    run_observation_once,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

pytestmark = pytest.mark.live_github
FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass(frozen=True)
class P2LiveConfig:
    owner: str
    repository: str
    token: str = field(repr=False)
    status_run_id: int
    cancel_run_id: int

    @property
    def repository_id(self) -> str:
        return f"github:{self.owner}/{self.repository}"


@dataclass
class RecordingLiveTransport:
    inner: UrllibGitHubJsonTransport = field(default_factory=UrllibGitHubJsonTransport)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    async def get_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubHttpResponse:
        self.requests.append(
            (
                url,
                {
                    key: "<redacted>" if key.casefold() == "authorization" else value
                    for key, value in headers.items()
                },
            )
        )
        return await self.inner.get_response(url, headers=headers)


def _positive_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is required for P2 live GitHub acceptance")
    try:
        value = int(raw)
    except ValueError:
        pytest.fail(f"{name} must be a positive integer")
    if value <= 0:
        pytest.fail(f"{name} must be a positive integer")
    return value


@pytest.fixture(scope="module")
def p2_live_config() -> P2LiveConfig:
    repository = os.getenv("CICD_P2_LIVE_GITHUB_REPOSITORY", "").strip()
    token = os.getenv("CICD_P2_LIVE_GITHUB_TOKEN", "").strip()
    if not repository or not token:
        pytest.skip("P2 live GitHub repository and token are required")
    parts = repository.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        pytest.fail("CICD_P2_LIVE_GITHUB_REPOSITORY must use owner/repository form")
    return P2LiveConfig(
        owner=parts[0].strip(),
        repository=parts[1].strip(),
        token=token,
        status_run_id=_positive_int("CICD_P2_LIVE_GITHUB_STATUS_RUN_ID"),
        cancel_run_id=_positive_int("CICD_P2_LIVE_GITHUB_CANCEL_RUN_ID"),
    )


def _registry(config: P2LiveConfig, transport: RecordingLiveTransport) -> ResolverRegistry:
    return ResolverRegistry(
        (
            GitHubCompositeResolver(
                owner=config.owner,
                repository=config.repository,
                token=config.token,
                transport=transport,
            ),
        )
    )


def _task(
    session,
    registry: ResolverRegistry,
    kind: EvidenceRequirementKind,
    run_id: int,
) -> InvestigationTask:
    subject_id = f"{session.analysis_state.repository.repository_id}:run:{run_id}"
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(kind, subject_id, qualifier=str(run_id)),
        kind=kind,
        provider=EvidenceProvider.GITHUB,
        subject_id=subject_id,
        qualifier=str(run_id),
        reason="P2 live status/dependency acceptance",
    )
    descriptor = registry.descriptor_for(kind)
    assert descriptor is not None
    session.investigation_state.requirements[requirement.requirement_id] = requirement
    task = InvestigationTask(
        task_id=stable_id(
            "task",
            InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE.value,
            requirement.requirement_id,
            descriptor.resolver_id,
        ),
        kind=InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE,
        subject_id=subject_id,
        requirement_id=requirement.requirement_id,
        resolver_id=descriptor.resolver_id,
        provider=descriptor.provider,
    )
    session.investigation_state.tasks[task.task_id] = task
    return task


async def _session_for_run(tmp_path: Path, run_id: int):
    repository = FIXTURES / "status-live"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / f"out-{run_id}",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    return repository, create_red_team_session(report.state, github_run_id=run_id)


@pytest.mark.asyncio
async def test_p2_status_dependency_semantics_live(
    tmp_path: Path,
    p2_live_config: P2LiveConfig,
) -> None:
    repository, session = await _session_for_run(tmp_path, p2_live_config.status_run_id)
    transport = RecordingLiveTransport()
    registry = _registry(p2_live_config, transport)

    identity_observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=_task(
            session,
            registry,
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
            p2_live_config.status_run_id,
        ),
    )
    assert isinstance(identity_observation.payload, WorkflowRunIdentityEvidence)
    assert identity_observation.payload.workflow_path == ".github/workflows/status.yml"

    jobs_observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=_task(
            session,
            registry,
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
            p2_live_config.status_run_id,
        ),
    )
    assert isinstance(jobs_observation.payload, WorkflowRunJobsEvidence)
    runtime_jobs = {job.name: job for job in jobs_observation.payload.jobs}
    assert runtime_jobs["Skipped"].conclusion == "skipped"
    assert runtime_jobs["Implicit after skip"].conclusion == "skipped"
    assert runtime_jobs["Always after skip"].conclusion == "success"
    assert runtime_jobs["Failing"].conclusion == "failure"
    assert runtime_jobs["Failure handler"].conclusion == "success"
    assert runtime_jobs["Explicit result"].conclusion == "success"
    assert runtime_jobs["Implicit after failure"].conclusion == "skipped"
    assert runtime_jobs["Middle"].conclusion == "success"
    assert runtime_jobs["Ancestor failure handler"].conclusion == "success"
    # 2026-08-29 hosted GitHub still exposes the historical needs.* transitive quirk: the
    # direct dependency Middle succeeded, yet the wildcard condition observed indirect failure.
    assert runtime_jobs["Wildcard indirect"].conclusion == "success"

    status_observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=_task(
            session,
            registry,
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS,
            p2_live_config.status_run_id,
        ),
    )
    assert isinstance(status_observation.payload, WorkflowRunStatusEvidence)
    assert status_observation.payload.conclusion == "failure"

    status_workflow = next(
        workflow
        for workflow in session.analysis_state.workflows
        if workflow.path.endswith("status.yml")
    )
    jobs = {job.job_key: job for job in status_workflow.jobs if job.job_key}
    contexts = {
        context.job_id: context for context in session.analysis_state.job_execution_contexts
    }
    assert contexts[jobs["implicit_after_skip"].job_id].reachable is TriState.FALSE
    assert contexts[jobs["always_after_skip"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["failure_handler"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["explicit_result"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["implicit_after_failure"].job_id].reachable is TriState.FALSE
    assert contexts[jobs["ancestor_failure_handler"].job_id].reachable is TriState.TRUE
    wildcard = contexts[jobs["wildcard_indirect"].job_id]
    assert wildcard.reachable is TriState.UNKNOWN
    assert wildcard.predicted_result is ExecutionResult.SUCCESS

    soft_fail = jobs["soft_fail"]
    steps = {step.name: step for step in soft_fail.steps if step.name}
    evaluations = {item.target_id: item for item in session.analysis_state.condition_evaluations}
    assert evaluations[steps["Outcome check"].step_id].verdict is TriState.UNKNOWN
    assert evaluations[steps["Conclusion check"].step_id].verdict is TriState.TRUE
    assert all(
        headers.get("Authorization") == "<redacted>"
        for _, headers in transport.requests
        if "Authorization" in headers
    )


@pytest.mark.asyncio
async def test_p2_cancellation_semantics_live(
    tmp_path: Path,
    p2_live_config: P2LiveConfig,
) -> None:
    repository, session = await _session_for_run(tmp_path, p2_live_config.cancel_run_id)
    transport = RecordingLiveTransport()
    registry = _registry(p2_live_config, transport)

    for kind in (
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS,
    ):
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.REPO_WRITER,
            task=_task(session, registry, kind, p2_live_config.cancel_run_id),
        )

    identity = next(
        payload
        for payload in session.knowledge_state.external_evidence.values()
        if isinstance(payload, WorkflowRunIdentityEvidence)
    )
    jobs_evidence = next(
        payload
        for payload in session.knowledge_state.external_evidence.values()
        if isinstance(payload, WorkflowRunJobsEvidence)
    )
    run_status = next(
        payload
        for payload in session.knowledge_state.external_evidence.values()
        if isinstance(payload, WorkflowRunStatusEvidence)
    )
    assert identity.workflow_path == ".github/workflows/cancel.yml"
    assert run_status.conclusion == "cancelled"
    runtime_jobs = {job.name: job for job in jobs_evidence.jobs}
    assert runtime_jobs["Long running"].conclusion == "cancelled"
    assert runtime_jobs["On cancel"].conclusion == "success"
    assert runtime_jobs["Always after cancel"].conclusion == "success"

    cancel_workflow = next(
        workflow
        for workflow in session.analysis_state.workflows
        if workflow.path.endswith("cancel.yml")
    )
    jobs = {job.job_key: job for job in cancel_workflow.jobs if job.job_key}
    contexts = {
        context.job_id: context for context in session.analysis_state.job_execution_contexts
    }
    assert contexts[jobs["long"].job_id].predicted_result is ExecutionResult.CANCELLED
    assert contexts[jobs["on_cancel"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["always_after_cancel"].job_id].reachable is TriState.TRUE
