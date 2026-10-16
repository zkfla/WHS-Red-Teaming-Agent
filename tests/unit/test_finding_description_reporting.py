"""Stored Finding descriptions survive display without creating reviewed details."""

import json

import pytest

from redteam.app.cli.commands.report import _render_markdown
from redteam.app.report_store import load_terminal_report, save_terminal_report
from redteam.orchestration.reporting import FindingProjection, TerminalReportProjection


def finding(**changes):
    return FindingProjection(
        finding_id="finding-fixture", title="Stored title", severity="medium",
        verified=True, evidence_ids=["ev-original"], **changes,
    )


def report(projected):
    return TerminalReportProjection(
        run_id="run", phase="finished", termination_reason="investigation_held",
        ready_for_release=False, actions_executed=0, finding_count=1,
        evidence_count=1, findings=[projected],
    )


def test_description_tail_and_evidence_references_survive_json_markdown_and_reload(tmp_path):
    description = "x" * 2400 + " This conclusion is limited to the stored observation."
    projected = report(finding(description=description))
    markdown = _render_markdown(projected)
    assert description in markdown and "ev-original" in markdown
    assert "Stored description:" in markdown and "Evidence references:" in markdown
    save_terminal_report(projected, reports_dir=tmp_path)
    restored = load_terminal_report("run", reports_dir=tmp_path)
    assert restored is not None
    assert restored.findings[0].description == description
    assert restored.model_dump_json() == projected.model_dump_json()
    assert _render_markdown(restored) == markdown
    assert restored.finding_count == 1 and restored.findings[0].verified


def test_old_finding_projection_round_trip_retains_wire_and_unknown_description():
    old = {"finding_id": "finding-fixture", "title": "Stored title", "severity": "medium",
           "verified": True, "evidence_ids": ["ev-original"]}
    restored = FindingProjection.model_validate(old)
    assert restored.description is None
    assert restored.model_dump(mode="json") == old
    assert json.loads(restored.model_dump_json()) == old
    assert "not recorded in this report" in _render_markdown(report(restored))


@pytest.mark.parametrize("description,rendered", [(None, "not recorded in this report"),
                                                 ("", "(empty)")])
def test_unknown_and_stored_empty_description_remain_distinct(description, rendered):
    projected = report(finding(description=description))
    assert rendered in _render_markdown(projected)
    payload = projected.model_dump(mode="json")["findings"][0]
    if description is None:
        assert "description" not in payload
    else:
        assert payload["description"] == ""


@pytest.mark.parametrize("bypass", [False, True])
def test_description_masks_secrets_at_input_serialization_and_rendering(bypass):
    text = "Stored description password=FindingSecretSentinel"
    item = finding(description="Safe initial text")
    if bypass:
        item = item.model_copy(update={"description": text})
    else:
        item = finding(description=text)
    projected = report(item)
    output = projected.model_dump_json() + _render_markdown(projected)
    assert "FindingSecretSentinel" not in output and "REDACTED" in output
    if bypass:
        assert item.description == text  # A display/export never edits its source model.


def test_description_markdown_is_literal_and_nested_exclusion_remains_supported():
    text = "`````\n## Untrusted heading\n<script>untrusted()</script>"
    projected = report(finding(description=text))
    markdown = _render_markdown(projected)
    assert "\n## Untrusted heading\n" not in markdown
    assert "`````` \"" in markdown and "\\n## Untrusted heading\\n" in markdown
    assert projected.findings[0].description == text
    filtered = projected.model_dump(exclude={"findings": {"__all__": {"description"}}})
    assert "description" not in filtered["findings"][0]
