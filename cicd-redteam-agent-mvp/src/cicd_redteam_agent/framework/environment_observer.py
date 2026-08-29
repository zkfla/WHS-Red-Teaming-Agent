from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    EnvironmentProtectionPolicy,
    InvestigationTask,
    InvestigationTaskKind,
    Observation,
    ObservationKind,
    ObservationSource,
    RedTeamSession,
)
from cicd_redteam_agent.domain.models import RepositoryWorkflowPermissionDefault


class EnvironmentObserver(Protocol):
    async def observe(
        self,
        session: RedTeamSession,
        task: InvestigationTask,
    ) -> Observation: ...


@dataclass(frozen=True)
class FakeRepositoryPermissionObserver:
    default_workflow_permissions: RepositoryWorkflowPermissionDefault

    async def observe(
        self,
        session: RedTeamSession,
        task: InvestigationTask,
    ) -> Observation:
        if task.kind is not InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS:
            raise ValueError(f"unsupported fake observation task: {task.kind}")
        observation_id = stable_id(
            "obs",
            session.analysis_state.repository.repository_id,
            task.task_id,
            self.default_workflow_permissions.value,
        )
        return Observation(
            observation_id=observation_id,
            task_id=task.task_id,
            kind=ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS,
            source=ObservationSource.FAKE,
            subject_id=task.subject_id,
            repository_default_workflow_permissions=self.default_workflow_permissions,
        )


@dataclass(frozen=True)
class FakeEnvironmentProtectionObserver:
    policy: EnvironmentProtectionPolicy

    async def observe(
        self,
        session: RedTeamSession,
        task: InvestigationTask,
    ) -> Observation:
        if task.kind is not InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY:
            raise ValueError(f"unsupported fake observation task: {task.kind}")
        if task.environment_name != self.policy.environment_name:
            raise ValueError("fake environment policy does not match the observation task")
        observation_id = stable_id(
            "obs",
            session.analysis_state.repository.repository_id,
            task.task_id,
            self.policy.model_dump_json(),
        )
        return Observation(
            observation_id=observation_id,
            task_id=task.task_id,
            kind=ObservationKind.ENVIRONMENT_PROTECTION_POLICY,
            source=ObservationSource.FAKE,
            subject_id=task.subject_id,
            environment_protection_policy=self.policy,
        )
