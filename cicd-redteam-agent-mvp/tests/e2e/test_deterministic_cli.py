import json
from pathlib import Path

from cicd_redteam_agent.cli import main

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_cli_writes_complete_outputs(tmp_path: Path) -> None:
    exit_code = main(["analyze", str(FIXTURES / "oidc-risk"), "--output", str(tmp_path)])
    assert exit_code == 0
    runs = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(runs) == 1
    assert {"analysis.json", "summary.md", "trace.jsonl"} <= {
        path.name for path in runs[0].iterdir()
    }
    data = json.loads((runs[0] / "analysis.json").read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETE"
    assert data["state"]["identities"][0]["trust_verified"] == "UNKNOWN"


def test_workflow_free_repository_is_complete(tmp_path: Path) -> None:
    repository = tmp_path / "empty-repository"
    repository.mkdir()
    output = tmp_path / "output"
    assert main(["analyze", str(repository), "--output", str(output)]) == 0
    run = next(output.iterdir())
    data = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    assert data["state"]["workflows"] == []
    assert data["state"]["findings"] == []


def test_prompt_injection_text_does_not_create_a_finding(tmp_path: Path) -> None:
    assert (
        main(
            [
                "analyze",
                str(FIXTURES / "prompt-injection"),
                "--output",
                str(tmp_path),
            ]
        )
        == 0
    )
    run = next(path for path in tmp_path.iterdir() if path.is_dir())
    data = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    assert data["state"]["findings"] == []
