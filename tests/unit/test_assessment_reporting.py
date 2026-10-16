"""Human-facing draft reports preserve proposals without asserting review authority."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from redteam.app.cli.commands.report import _render_markdown
from redteam.app.report_store import load_terminal_report, save_terminal_report
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.mission import InvestigationResult
from redteam.orchestration.reporting import TerminalReportProjection, investigation_report_views
from tests.fakes.assessment_drafts import draft


def investigation(**changes):
    payload = draft(
        evidence_id="ev-original", judgment="supports", severity="high",
        impact="Proposed effect only", alternative_explanations=["An intended public response"],
        limitation_refs=["ev-diagnostic"],
    )
    payload["observations"][0]["sources"][0].update(
        raw_path=["body_preview"], text_span={"offset": 700, "length": 40}, role="refute",
    )
    return {"task_id": "task", "status": "completed", "required": False,
            "result": InvestigationResult(
                result_id="result", outcome="inconclusive", summary="No candidate verdict",
                contract_version=2, completion_receipt_id="task-review",
                assessment_drafts=[AssessmentDraft.model_validate(payload)],
            ).model_dump(mode="json"), **changes}


def report(items):
    return TerminalReportProjection(
        run_id="run", phase="finished", termination_reason="investigation_held",
        ready_for_release=False, actions_executed=0, finding_count=0, evidence_count=0,
        investigations=items,
    )


def test_report_projects_complete_draft_and_exact_identity_without_counting_a_finding(tmp_path):
    item = investigation()
    original = deepcopy(item)
    projected = report([item])
    safe = projected.investigations[0]
    saved_draft = AssessmentDraft.model_validate(safe["result"]["assessment_drafts"][0])
    assert safe["assessment_draft_refs"] == [{
        **saved_draft.identity("result"), "contract_version": 1, "review_binding": "unknown",
    }]
    text = _render_markdown(projected)
    for content in [
        saved_draft.name, saved_draft.statement, saved_draft.reason,
        'Proposed classification: ` "Unclassified" `', 'proposed severity: ` "high" `',
        "Worker opinion: supports", "Worker-declared expectation (assumption)",
        "Proposed effect only", "An intended public response", "ev-original", "ev-diagnostic",
        '"offset": 700', '"role": "refute"', "not verified Findings",
        "Independent draft review binding: unknown", "Task completion receipt:", "task-review",
        "does not establish approval of these drafts",
        saved_draft.identity("result")["content_hash"],
    ]:
        assert content in text
    assert "- Findings: 0" in text and projected.findings == []
    save_terminal_report(projected, reports_dir=tmp_path)
    restored = load_terminal_report("run", reports_dir=tmp_path)
    assert restored is not None
    assert restored.model_dump(mode="json") == projected.model_dump(mode="json")
    assert _render_markdown(restored) == text
    assert item == original


def test_long_claim_is_rendered_with_its_end_and_original_hash():
    item = investigation()
    statement = "a" * 700 + " The observation does not establish a security violation."
    item["result"]["assessment_drafts"][0]["statement"] = statement
    projected = report([item])
    assert statement in _render_markdown(projected)
    expected = AssessmentDraft.model_validate(item["result"]["assessment_drafts"][0])
    assert projected.investigations[0]["assessment_draft_refs"][0]["content_hash"] == (
        expected.identity("result")["content_hash"]
    )


def test_report_normalizes_bypassed_input_on_serialization_and_rendering():
    original = report([investigation()])
    injected = deepcopy(original.investigations)
    injected[0]["result"]["assessment_drafts"][0]["statement"] = "Claim password=ReportSentinel"
    injected[0]["assessment_draft_refs"][0].update(
        content_hash="outdated-hash", review_binding="approved",
    )
    bypassed = original.model_copy(update={"investigations": injected})
    rendered = bypassed.model_dump_json() + _render_markdown(bypassed)
    assert "ReportSentinel" not in rendered and "REDACTED" in rendered
    assert "outdated-hash" not in rendered and '"review_binding":"approved"' not in rendered
    assert injected[0]["assessment_draft_refs"][0]["content_hash"] == "outdated-hash"


@pytest.mark.parametrize("result", [None, {"summary": "Old summary, not a measured observation"}])
def test_old_report_gains_no_draft_or_inferred_fact(result):
    item = investigation(result=result)
    projected = report([item])
    assert projected.investigations == [item]
    assert "Worker assessment proposals" not in _render_markdown(projected)
    assert "assessment_draft_refs" not in projected.model_dump_json()


def test_malformed_draft_is_rejected_without_echoing_secret_validation_input():
    item = investigation()
    item["result"]["assessment_drafts"][0].update(
        judgment="invalid", statement="password=HiddenValidationSentinel",
    )
    with pytest.raises(ValidationError) as caught:
        report([item])
    assert "HiddenValidationSentinel" not in str(caught.value)


def test_public_view_is_idempotent_and_discards_detached_draft_references():
    once = investigation_report_views([investigation()])
    assert investigation_report_views(once) == once
    once[0]["result"]["assessment_drafts"] = []
    assert "assessment_draft_refs" not in investigation_report_views(once)[0]


def test_report_serializer_preserves_nested_field_exclusion():
    projected = report([investigation()])
    filtered = projected.model_dump(exclude={
        "investigations": {"__all__": {"result": {"assessment_drafts"}}},
    })
    assert "assessment_drafts" not in filtered["investigations"][0]["result"]


def test_worker_text_cannot_create_a_report_heading_or_close_its_literal_span():
    item = investigation()
    text = "Worker text with `````\n## Injected report heading\n<b>untrusted</b>"
    item["result"]["assessment_drafts"][0]["statement"] = text
    projected = report([item])
    markdown = _render_markdown(projected)
    assert "\n## Injected report heading\n" not in markdown
    assert "`````` \"Worker text" in markdown
    assert "\\n## Injected report heading\\n" in markdown
    assert projected.investigations[0]["result"]["assessment_drafts"][0]["statement"] == text
