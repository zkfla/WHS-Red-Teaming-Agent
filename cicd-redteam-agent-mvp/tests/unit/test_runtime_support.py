from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.investigation import (
    DeploymentBranchPolicyMode,
    EnvironmentProtectionPolicy,
    ExternalTransportCacheEntry,
    InvestigationTask,
    InvestigationTaskKind,
    InvestigationTaskStatus,
    Observation,
    ObservationKind,
    ObservationSource,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, RepositoryWorkflowPermissionDefault
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    lease_task,
)
from cicd_redteam_agent.framework.runtime_support import (
    AgentRuntimeMetrics,
    CheckpointWriter,
    EventJournal,
    RecoverableModelOutputError,
    RetryPolicy,
    RunLease,
    call_model_with_retry,
    is_transient_model_error,
    load_checkpoint,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"status={status_code}")


def test_retry_classification_and_full_jitter_are_bounded() -> None:
    assert is_transient_model_error(StatusError(429))
    assert is_transient_model_error(StatusError(503))
    assert not is_transient_model_error(StatusError(401))
    assert not is_transient_model_error(ValueError("invalid capability schema"))
    assert is_transient_model_error(RecoverableModelOutputError("malformed tool JSON"))
    policy = RetryPolicy(initial_delay=2.0, max_delay=5.0)
    assert policy.delay(1, random_value=0.5) == 1.0
    assert policy.delay(4, random_value=1.0) == 5.0


@pytest.mark.asyncio
async def test_transient_failure_retries_without_nested_model_state() -> None:
    attempts = 0
    sleeps: list[float] = []
    metrics = AgentRuntimeMetrics()

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(429)
        return "ok"

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await call_model_with_retry(
        operation,
        policy=RetryPolicy(max_attempts=3, initial_delay=0, max_delay=0),
        metrics=metrics,
        time_remaining=lambda: timedelta(seconds=10),
        sleep=no_sleep,
    )

    assert result == "ok"
    assert attempts == 3
    assert metrics.model_attempts == 3
    assert metrics.retries == 2
    assert sleeps == [0.0, 0.0]


@pytest.mark.asyncio
async def test_non_retryable_failure_is_raised_immediately() -> None:
    metrics = AgentRuntimeMetrics()

    with pytest.raises(ValueError, match="invalid api key"):
        await call_model_with_retry(
            lambda: (_ for _ in ()).throw(ValueError("invalid api key")),
            policy=RetryPolicy(),
            metrics=metrics,
            time_remaining=lambda: timedelta(seconds=10),
        )

    assert metrics.model_attempts == 1
    assert metrics.retries == 0


def test_checkpoint_replaces_complete_json_without_temp_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    metrics = AgentRuntimeMetrics()
    checkpoint_path = tmp_path / "run" / "checkpoint.json"
    journal = EventJournal(tmp_path / "run" / "events.jsonl", attempt_id=1)
    writer = CheckpointWriter(
        checkpoint_path,
        state,
        "fake",
        "fake-model",
        metrics,
        journal=journal,
        config={
            "attacker_profile": "fork-pr",
            "analysis_mode": "reasoning",
            "max_rounds": 5,
            "max_tokens": 100,
            "max_cost": 1,
            "max_duration": 30,
        },
    )

    writer.write(rounds=1, tokens=10, cost=0.1, reason="round_complete")
    state.pending_questions.append("later state")
    writer.write(rounds=2, tokens=20, cost=0.2, reason="proposal_validated")

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert payload["rounds"] == 2
    assert payload["reason"] == "proposal_validated"
    assert payload["state"]["pending_questions"] == ["later state"]
    assert payload["runtime_metrics"]["checkpoints_written"] == 2
    assert payload["schema_version"] == "2.0"
    assert payload["resume_phase"] == "TERMINAL"
    assert payload["committed_event_seq"] == 2
    restored = load_checkpoint(checkpoint_path)
    assert restored.rounds == 2
    assert restored.state.pending_questions == ["later state"]
    assert not list(checkpoint_path.parent.glob("*.tmp"))


def test_checkpoint_round_trips_canonical_investigation_session(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    session = create_red_team_session(state)
    task = InvestigationTask(
        task_id="task_1111111111111111",
        kind=InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS,
        subject_id=state.repository.repository_id,
        status=InvestigationTaskStatus.COMPLETED,
        attempts=1,
    )
    observation = Observation(
        observation_id="obs_2222222222222222",
        task_id=task.task_id,
        kind=ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS,
        source=ObservationSource.FAKE,
        subject_id=task.subject_id,
        repository_default_workflow_permissions=RepositoryWorkflowPermissionDefault.READ,
    )
    session.investigation_state.tasks[task.task_id] = task
    session.investigation_state.observations[observation.observation_id] = observation
    session.knowledge_state.repository_default_workflow_permissions = (
        RepositoryWorkflowPermissionDefault.READ
    )
    environment_policy = EnvironmentProtectionPolicy(
        environment_name="production",
        required_reviewer_count=1,
        deployment_branch_policy=DeploymentBranchPolicyMode.PROTECTED,
    )
    session.knowledge_state.environment_protection_policies["production"] = environment_policy
    session.knowledge_state.revision = 2
    cache_key = "https://api.github.com/repos/example/repo/actions/permissions/workflow"
    session.investigation_state.transport_cache[cache_key] = ExternalTransportCacheEntry(
        provider=EvidenceProvider.GITHUB,
        cache_key=cache_key,
        payload={"default_workflow_permissions": "read"},
        etag='"workflow-v1"',
    )
    state.pending_questions.append("state changed after session creation")
    metrics = AgentRuntimeMetrics(
        external_network_requests=3,
        external_cache_hits=2,
        external_not_modified_hits=1,
        external_retries=1,
        external_rate_limit_remaining=4_997,
    )

    checkpoint_path = tmp_path / "run" / "checkpoint.json"
    CheckpointWriter(
        checkpoint_path,
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        metrics,
        config={"observe_github": True},
        investigation_session=session,
    ).write(rounds=1, tokens=10, cost=0.1, reason="round_complete")

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert payload["investigation_session"]["schema_version"] == "1.0"
    restored = load_checkpoint(checkpoint_path)
    restored_session = restored.investigation_session
    assert restored_session is not None
    assert restored_session.analysis_state.pending_questions == [
        "state changed after session creation"
    ]
    assert restored_session.knowledge_state.revision == 2
    assert restored_session.knowledge_state.environment_protection_policies == {
        "production": environment_policy
    }
    assert restored_session.investigation_state.tasks[task.task_id].attempts == 1
    assert (
        restored_session.investigation_state.observations[observation.observation_id].task_id
        == task.task_id
    )
    assert restored_session.investigation_state.active_lease is None
    assert restored_session.investigation_state.transport_cache[cache_key].etag == '"workflow-v1"'
    assert restored.runtime_metrics.external_network_requests == 3
    assert restored.runtime_metrics.external_cache_hits == 2
    assert restored.runtime_metrics.external_not_modified_hits == 1
    assert restored.runtime_metrics.external_retries == 1
    assert restored.runtime_metrics.external_rate_limit_remaining == 4_997


def test_checkpoint_rejects_an_active_investigation_lease(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    session = create_red_team_session(state)
    task = InvestigationTask(
        task_id="task_3333333333333333",
        kind=InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS,
        subject_id=state.repository.repository_id,
    )
    session.investigation_state.tasks[task.task_id] = task
    lease_task(session, task)
    writer = CheckpointWriter(
        tmp_path / "run" / "checkpoint.json",
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={"observe_github": True},
        investigation_session=session,
    )

    with pytest.raises(ValueError, match="lease is active"):
        writer.write(rounds=0, tokens=0, cost=0, reason="round_complete")


def test_observation_checkpoint_without_session_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    checkpoint_path = tmp_path / "run" / "checkpoint.json"
    CheckpointWriter(
        checkpoint_path,
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={"observe_github": False},
    ).write(rounds=1, tokens=1, cost=0, reason="model_error")
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["config"]["observe_github"] = True
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="has no investigation session"):
        load_checkpoint(checkpoint_path)


def test_checkpoint_rejects_a_session_with_divergent_analysis_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    session = create_red_team_session(state)
    checkpoint_path = tmp_path / "run" / "checkpoint.json"
    CheckpointWriter(
        checkpoint_path,
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={"observe_github": True},
        investigation_session=session,
    ).write(rounds=1, tokens=1, cost=0, reason="model_error")
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["investigation_session"]["session"]["analysis_state"]["pending_questions"] = [
        "tampered"
    ]
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical analysis state"):
        load_checkpoint(checkpoint_path)


def test_event_journal_repairs_only_a_malformed_final_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    journal = EventJournal(path, attempt_id=1)
    journal.append("RUN_STARTED")
    with path.open("ab") as stream:
        stream.write(b'{"partial":')

    resumed = EventJournal(path, attempt_id=2)
    assert [event.event_type for event in resumed.events] == ["RUN_STARTED"]
    resumed.append("RESUME_STARTED")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]


def test_event_journal_rejects_corruption_before_final_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    journal = EventJournal(path, attempt_id=1)
    journal.append("RUN_STARTED")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
        stream.write("still-not-json\n")

    with pytest.raises(ValueError, match="corrupt before its final line"):
        EventJournal(path, attempt_id=2)


def test_run_lease_rejects_an_active_run(tmp_path: Path) -> None:
    path = tmp_path / ".run.lock"
    with RunLease(path):
        with pytest.raises(ValueError, match="already active"):
            with RunLease(path):
                pass
    assert not path.exists()
