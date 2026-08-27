from cicd_redteam_agent.cli import build_parser
from cicd_redteam_agent.domain.models import FindingStatus
from cicd_redteam_agent.domain.reasoning_evaluation import ReasoningExpectation


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
