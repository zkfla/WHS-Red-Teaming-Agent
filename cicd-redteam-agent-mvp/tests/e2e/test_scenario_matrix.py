import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository

FIXTURES = Path(__file__).parents[1] / "fixtures"
SCENARIOS = [
    "gha-01-shell-injection",
    "gha-03-artifact-trust",
    "gha-04-mutable-action",
    "gha-05-token-permissions",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("variant", ["vulnerable", "secure"])
async def test_scenario_matches_expected_result(
    scenario: str, variant: str, tmp_path: Path
) -> None:
    repository = FIXTURES / scenario / variant
    expected = json.loads((repository / "expected.json").read_text(encoding="utf-8"))
    report, _ = await analyze_repository(repository, AnalysisConfig(output_dir=tmp_path))
    actual = {finding.rule_id: finding.status.value for finding in report.state.findings}

    for finding in expected["expected_findings"]:
        assert actual.get(finding["rule_id"]) == finding["status"]
    for rule_id in expected["forbidden_rule_ids"]:
        assert rule_id not in actual
