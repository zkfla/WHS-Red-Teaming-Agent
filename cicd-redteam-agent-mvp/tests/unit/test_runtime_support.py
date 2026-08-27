from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from cicd_redteam_agent.application.state import create_initial_state
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
