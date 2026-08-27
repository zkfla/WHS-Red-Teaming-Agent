from pathlib import Path

from cicd_redteam_agent.application.evaluate import collect_agent_behavior
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.cli import build_parser
from cicd_redteam_agent.domain.models import (
    AgentWorkingMemory,
    AnalysisReport,
    FindingStatus,
    ReportStatus,
    Termination,
    TraceMetadata,
    WorkingHypothesis,
    WorkingHypothesisStatus,
)
from cicd_redteam_agent.domain.reasoning_evaluation import ReasoningExpectation
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def test_fixture_oracle_parses_relation_verdicts_and_path_focus() -> None:
    expectation = ReasoningExpectation.from_dict(
        {
            "scenario_ids": ["GHA-TEST-A", "GHA-TEST-B"],
            "variant": "candidate",
            "expected_status": "CANDIDATE",
            "required_relations": [
                ["source_relation", "TRUE"],
                ["unknown_relation", "UNKNOWN"],
            ],
            "path_relations": ["unknown_relation"],
        }
    )
    assert expectation.expected_status is FindingStatus.CANDIDATE
    assert expectation.required_relation_types == {
        "source_relation",
        "unknown_relation",
    }
    assert expectation.required_relation_verdicts == {
        ("source_relation", "TRUE"),
        ("unknown_relation", "UNKNOWN"),
    }
    assert expectation.path_relation_types == {"unknown_relation"}


def test_evaluate_cli_accepts_hidden_matrix_filters() -> None:
    args = build_parser().parse_args(
        [
            "evaluate",
            "tests/fixtures/reasoning-matrix",
            "--output",
            "output",
            "--provider",
            "codex-chatgpt",
            "--scenario",
            "gha-01",
            "--variant",
            "candidate",
        ]
    )
    assert args.command == "evaluate"
    assert args.scenario == ["gha-01"]
    assert args.variant == ["candidate"]


def test_collect_agent_behavior_uses_only_observable_state(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    state.repeated_calls = {"ListWorkflows:{}": 2, "SubmitAnalysis:{}": 1}
    state.working_memory = AgentWorkingMemory(
        revision=2,
        hypotheses=[
            WorkingHypothesis(
                hypothesis_id="hyp_0123456789abcdef",
                claim="A relation may form an attack path",
                status=WorkingHypothesisStatus.READY_FOR_PROPOSAL,
                decision_summary="Grounding is ready for deterministic validation.",
                created_revision=1,
                updated_revision=2,
            )
        ],
    )
    state.termination = Termination(
        reason="proposal_validated",
        rounds=3,
        tokens=1200,
        duration_seconds=4.5,
    )
    report = AnalysisReport(
        status=ReportStatus.COMPLETE,
        termination_reason="proposal_validated",
        state=state,
        trace=TraceMetadata(framework_commit="test"),
        run_metadata={
            "agent_runtime": {
                "model_attempts": 3,
                "retries": 0,
                "context_compactions": 1,
            }
        },
    )

    behavior = collect_agent_behavior(report)

    assert behavior["rounds"] == 3
    assert behavior["tokens"] == 1200
    assert behavior["capability_calls"] == 3
    assert behavior["unique_capability_calls"] == 2
    assert behavior["repeated_capability_calls"] == 1
    assert behavior["working_memory_revision"] == 2
    assert behavior["hypothesis_statuses"] == {"READY_FOR_PROPOSAL": 1}
