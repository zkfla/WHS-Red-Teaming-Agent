"""Actual handlers, reopened SQLite, read-only report CLI and synthetic stored data."""

import argparse
import json

import pytest

from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.report import _render_markdown, run_generate
from redteam.app.report_store import load_terminal_report, save_terminal_report
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.orchestration.central_results import publish_result_handler, settle_central
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.local_tools import LocalToolContext
from tests.fakes.assessment_drafts import draft
from tests.integration.test_assessment_result_storage import owned_original, publication
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import selection


@pytest.mark.parametrize("intermediate", [False, True])
async def test_stored_proposal_reopens_and_reaches_json_and_markdown_without_model_calls(
    tmp_path, intermediate,
):
    async with selection(tmp_path) as (session, llm, tools):
        task_id, runtime, original = await owned_original(session, candidate=True)
        context = LocalToolContext(session.deps, runtime)
        payload = draft(
            target=TARGET, evidence_id=str(original.evidence_id), hypothesis_id="candidate",
            judgment="supports", severity="high", reason="Proposal password=ReportPrivateSentinel",
            statement="a" * 700 + " This is not a verified security conclusion.",
            limitations=["Original body was not retained; only a transformed preview exists."],
            alternative_explanations=["The intended boundary has not been established."],
        )
        graph = AttackGraphService(session.deps.graph_store)
        candidate_before = await graph.get_hypothesis("candidate")
        if intermediate:
            assert (await publish_result_handler(publication([payload]), context)).ok
            published = (await session.deps.central_state.get_task(task_id)).result
        response = await settle_central("task", CompletionProposal.model_validate({
            "outcome": "inconclusive", "reason": "Store partial observations, no verdict",
            "assessment_drafts": [] if intermediate else [payload],
        }), context)
        assert response.ok, response.error
        saved = await session.deps.central_state.get_task(task_id)
        if intermediate:
            assert saved.result.result_id == published.result_id
            assert saved.result.assessment_drafts == published.assessment_drafts
        records_before = await session.deps.evidence_store.list_for_run(runtime.run_id)
        expected = AssessmentDraft.model_validate(payload)
        expected_ref = {**expected.identity(saved.result.result_id), "contract_version": 1,
                        "review_binding": "unknown"}
        # This reader opens fresh stores, without provider, checkpoint or recovery.
        reopened, _, report = await inspect_central_run(
            session.deps.central_state.path, str(runtime.run_id),
        )
        investigation, = report.investigations
        assert investigation["result"]["assessment_drafts"] == [expected.model_dump(mode="json")]
        assert investigation["assessment_draft_refs"] == [expected_ref]
        assert report.finding_count == 0 and report.findings == []
        candidate, = report.candidates
        assert candidate.status == str(candidate_before.status)
        save_terminal_report(report, reports_dir=tmp_path / "snapshots")
        restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path / "snapshots")
        assert restored is not None and restored.investigations == report.investigations
        assert "not a verified security conclusion" in _render_markdown(restored)
        for format_name in ("md", "json"):
            assert await run_generate(argparse.Namespace(
                run_id=str(runtime.run_id), state_db=str(session.deps.central_state.path),
                format=format_name, output_dir=str(tmp_path / "exports"),
            )) == 0
        exported = tmp_path / "exports" / str(runtime.run_id)
        json_report = json.loads((exported / "report.json").read_text())
        text_report = (exported / "report.md").read_text()
        assert json_report["investigations"][0]["assessment_draft_refs"] == [expected_ref]
        for text in (json.dumps(json_report), text_report):
            assert "ReportPrivateSentinel" not in text
            assert "Retained safe text." not in text
            assert "Original body was not retained" in text
            assert "transformed preview" in text
        assert expected.identity(saved.result.result_id)["content_hash"] in text_report
        assert content_hash(await reopened.get_task(task_id)) == content_hash(saved)
        assert await session.deps.evidence_store.list_for_run(runtime.run_id) == records_before
        assert await graph.get_hypothesis("candidate") == candidate_before
        assert await graph.list_findings() == []
        assert not llm.requests and not tools.inner.invocations
