import json
from pathlib import Path

from whs_redteam.reporting import build_markdown_report, write_json_report, write_markdown_report


def test_report_rendering_keeps_machine_verdict_and_redacts_payloads(tmp_path: Path) -> None:
    report = {
        "run_id": "run-1",
        "engagement_name": "demo",
        "planning": {
            "provider": "rules",
            "planner_round": 1,
            "fallback_used": False,
            "selected_candidate_ids": ["sql-id"],
        },
        "exploitability_decision": {
            "verdict": "CONFIRMED",
            "confirmed_findings": 1,
            "inconclusive_findings": 0,
            "rationale": "bounded probe",
        },
        "findings": [
            {
                "category": "sqli",
                "status": "CONFIRMED",
                "severity": "high",
                "title": "SQL injection",
                "summary": "An oracle was reproduced.",
                "target_url": "https://example.test/items",
                "negative_control_passed": True,
            }
        ],
        "injection_tests": [
            {"fixture_id": "sql-id", "confirmed": True, "confirmed_modes": ["boolean_signal"]}
        ],
    }
    markdown = build_markdown_report(
        report,
        ai_explanation={
            "summary": "The machine verdict is confirmed.",
            "limitations": "This is a bounded test.",
            "next_steps": "Fix the query construction.",
        },
    )
    assert "CONFIRMED" in markdown
    assert "AI explanation" in markdown
    assert "1 OR 1" not in markdown
    json_path = write_json_report(tmp_path / "findings.json", report)
    md_path = write_markdown_report(tmp_path / "findings.md", report)
    assert json.loads(json_path.read_text())["run_id"] == "run-1"
    assert md_path.exists()
