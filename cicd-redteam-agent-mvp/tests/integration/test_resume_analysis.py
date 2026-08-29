import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import ResumeLimitOverrides, resume_analysis
from cicd_redteam_agent.application.objective import compile_objective_goal, objective_hash
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.models import ReportStatus
from cicd_redteam_agent.framework.hackingbuddy_adapter import AgentRunResult
from cicd_redteam_agent.framework.investigation_runtime import create_red_team_session
from cicd_redteam_agent.framework.runtime_support import (
    AgentRuntimeMetrics,
    CheckpointWriter,
    EventJournal,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


@pytest.mark.asyncio
async def test_resume_continues_in_the_same_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    state.pending_questions = ["saved question"]
    run_dir = tmp_path / "output" / "run-1"
    metrics = AgentRuntimeMetrics(model_attempts=2, no_progress_rounds=1)
    journal = EventJournal(run_dir / "events.jsonl", attempt_id=1)
    journal.append("RUN_STARTED")
    investigation_session = create_red_team_session(state, github_run_id=4242)
    checkpoint = CheckpointWriter(
        run_dir / "checkpoint.json",
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        metrics,
        journal=journal,
        attempt_id=1,
        config={
            "attacker_profile": "fork-pr",
            "analysis_mode": "reasoning",
            "max_rounds": 3,
            "max_tokens": 100,
            "max_cost": 1.0,
            "max_duration": 30,
            "repository_commit": None,
            "repository_dirty": None,
            "objective": "inspect OIDC",
            "objective_hash": objective_hash("inspect OIDC"),
            "objective_goal": compile_objective_goal("inspect OIDC").as_dict(),
            "observe_github": True,
            "github_run_id": 4242,
        },
        investigation_session=investigation_session,
    )
    checkpoint.write(
        rounds=1,
        tokens=10,
        cost=0.1,
        reason="model_error",
        active_duration_seconds=2.0,
    )

    async def fake_run_agent_analysis(
        resumed_state, boundary, config, actual_run_dir, *, resume_checkpoint
    ) -> AgentRunResult:
        assert actual_run_dir == run_dir.resolve()
        assert boundary.root == repository.resolve()
        assert resume_checkpoint.rounds == 1
        assert resume_checkpoint.runtime_metrics.model_attempts == 2
        assert config.max_rounds == 5
        assert config.objective == "inspect OIDC"
        assert config.observe_github is True
        assert config.github_run_id == 4242
        assert resume_checkpoint.investigation_session is not None
        assert resume_checkpoint.investigation_session.github_run_id == 4242
        resumed_state.pending_questions.append("resumed")
        return AgentRunResult(
            status=ReportStatus.COMPLETE,
            reason="proposal_validated",
            rounds=2,
            tokens=20,
            cost=0.2,
            warnings=[],
            provider="codex-chatgpt",
            runtime_metrics={"model_attempts": 3},
            attempt_id=2,
            duration_seconds=3.0,
        )

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.hackingbuddy_adapter.run_agent_analysis",
        fake_run_agent_analysis,
    )

    report, actual_run_dir = await resume_analysis(
        run_dir,
        limits=ResumeLimitOverrides(max_rounds=5),
    )

    assert actual_run_dir == run_dir.resolve()
    assert report.status is ReportStatus.COMPLETE
    assert report.state.pending_questions == ["saved question", "resumed"]
    assert (run_dir / "analysis.json").exists()
    assert (run_dir / "summary.md").exists()


@pytest.mark.asyncio
async def test_resume_rejects_a_changed_repository_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    state.repository.git_commit = "saved-commit"
    run_dir = tmp_path / "output" / "run-1"
    checkpoint = CheckpointWriter(
        run_dir / "checkpoint.json",
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={
            "attacker_profile": "fork-pr",
            "analysis_mode": "reasoning",
            "max_rounds": 3,
            "max_tokens": 100,
            "max_cost": 1.0,
            "max_duration": 30,
            "objective": None,
            "objective_hash": None,
            "objective_goal": compile_objective_goal(None).as_dict(),
        },
    )
    checkpoint.write(rounds=1, tokens=1, cost=0, reason="model_error")

    with pytest.raises(ValueError, match="repository commit changed"):
        await resume_analysis(run_dir)


@pytest.mark.asyncio
async def test_resume_rejects_tampered_objective(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    run_dir = tmp_path / "output" / "run-1"
    checkpoint = CheckpointWriter(
        run_dir / "checkpoint.json",
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={
            "attacker_profile": "fork-pr",
            "analysis_mode": "reasoning",
            "max_rounds": 3,
            "max_tokens": 100,
            "max_cost": 1.0,
            "max_duration": 30,
            "objective": "inspect OIDC",
            "objective_hash": objective_hash("inspect OIDC"),
            "objective_goal": compile_objective_goal("inspect OIDC").as_dict(),
        },
    )
    checkpoint.write(rounds=1, tokens=1, cost=0, reason="model_error")
    payload = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    payload["config"]["objective"] = "inspect cache"
    (run_dir / "checkpoint.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="objective hash mismatch"):
        await resume_analysis(run_dir)


@pytest.mark.asyncio
async def test_resume_rejects_tampered_compiled_objective_goal(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    run_dir = tmp_path / "output" / "run-1"
    objective = "inspect OIDC"
    checkpoint = CheckpointWriter(
        run_dir / "checkpoint.json",
        state,
        "codex-chatgpt",
        "gpt-5.6-luna",
        AgentRuntimeMetrics(),
        config={
            "attacker_profile": "fork-pr",
            "analysis_mode": "reasoning",
            "max_rounds": 3,
            "max_tokens": 100,
            "max_cost": 1.0,
            "max_duration": 30,
            "objective": objective,
            "objective_hash": objective_hash(objective),
            "objective_goal": compile_objective_goal(objective).as_dict(),
        },
    )
    checkpoint.write(rounds=1, tokens=1, cost=0, reason="model_error")
    payload = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    payload["config"]["objective_goal"]["mechanism_hints"] = ["cache"]
    (run_dir / "checkpoint.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="compiled objective goal mismatch"):
        await resume_analysis(run_dir)
