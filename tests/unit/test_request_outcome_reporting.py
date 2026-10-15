"""Request failure categories survive report storage without inventing old causes."""

from redteam.app.cli.commands.report import _render_markdown
from redteam.core.enums import AgentPhase
from redteam.orchestration.reporting import TerminalReportProjection


def report_with(accounting):
    return TerminalReportProjection(
        run_id="run-report", phase=AgentPhase.FAILED, termination_reason="planning_error",
        ready_for_release=False, actions_executed=0, finding_count=0, evidence_count=0,
        llm_request_accounting=accounting,
    )


def test_report_roundtrip_distinguishes_unknown_dispatch_from_unreported_usage():
    report = report_with({
        "spent": 4, "reserved": 0, "known_tokens": 20, "unknown_usage": 3,
        "unknown_dispatch": 2,
        "unknown_outcomes": {"provider_failure": 1, "cancelled": 1},
    })
    restored = TerminalReportProjection.model_validate_json(report.model_dump_json())
    rendered = _render_markdown(restored)
    assert "unknown usage: 3" in rendered
    assert "Dispatched requests with unknown outcome: 2" in rendered
    assert '"cancelled": 1' in rendered and '"provider_failure": 1' in rendered


def test_legacy_report_does_not_invent_failure_categories():
    rendered = _render_markdown(report_with({
        "spent": 1, "reserved": 0, "known_tokens": 0, "unknown_usage": 1,
    }))
    assert "Recorded unknown-outcome categories" not in rendered
    assert "Dispatched requests with unknown outcome" not in rendered
