"""Seeded Finding storage/reporting, not vulnerability validation or promotion."""

import argparse
import json

import pytest

from redteam.app.bootstrap import build_central_graph
from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.findings import run_list
from redteam.app.cli.commands.report import run_generate
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.graph import Finding
from redteam.orchestration.coordination import content_hash
from tests.integration.test_assessment_result_storage import owned_original
from tests.integration.test_probe_workers import selection


@pytest.mark.parametrize("verified", [False, True])
async def test_stored_finding_description_reopens_and_reaches_all_json_exports(
    tmp_path, capsys, verified,
):
    async with selection(tmp_path) as (session, llm, tools):
        _, runtime, original = await owned_original(session)
        graph = AttackGraphService(session.deps.graph_store)
        # An existing stored record is the fixture, not an independently reviewed
        # security result. No reviewer, candidate transition or promoter is invoked.
        seeded = Finding(
            finding_id="finding-description-fixture", title="Stored finding fixture",
            description="a" * 2400 + " Qualification after the old display limits. "
                        "password=StoredFindingPrivateSentinel",
            severity="medium", verified=verified, evidence_ids=[str(original.evidence_id)],
            metadata={"unreviewed_details": {
                "severity": "critical", "impact": "UnadoptedMetadataSentinel",
            }},
        )
        await graph.record_finding(seeded)
        stored = await graph.get_finding(seeded.finding_id)
        records_before = await session.deps.evidence_store.list_for_run(runtime.run_id)
        task_before = await session.deps.central_state.list_tasks()
        reopened, _, projected = await inspect_central_run(
            session.deps.central_state.path, str(runtime.run_id),
        )
        finding, = projected.findings
        assert finding.description is not None and "Qualification after" in finding.description
        assert finding.severity == seeded.severity and finding.verified == verified
        assert projected.finding_count == 1
        assert finding.evidence_ids == [str(original.evidence_id)]
        for format_name in ("md", "json"):
            assert await run_generate(argparse.Namespace(
                run_id=str(runtime.run_id), state_db=str(session.deps.central_state.path),
                format=format_name, output_dir=str(tmp_path / "exports"),
            )) == 0
        capsys.readouterr()
        assert await run_list(argparse.Namespace(
            run_id=str(runtime.run_id), state_db=str(session.deps.central_state.path), json=True,
        )) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed[0]["description"] == finding.description
        exported = tmp_path / "exports" / str(runtime.run_id)
        report_json = json.loads((exported / "report.json").read_text())
        report_md = (exported / "report.md").read_text()
        assert report_json["findings"][0]["description"] == finding.description
        assert finding.description in report_md
        for text in (json.dumps(listed), json.dumps(report_json), report_md):
            assert "StoredFindingPrivateSentinel" not in text
            assert "UnadoptedMetadataSentinel" not in text
            assert "Retained safe text." not in text
        reread = await AttackGraphService(build_central_graph(reopened)).get_finding(
            seeded.finding_id,
        )
        assert content_hash(reread) == content_hash(stored)
        assert await session.deps.central_state.list_tasks() == task_before
        assert await session.deps.evidence_store.list_for_run(runtime.run_id) == records_before
        assert await session.deps.central_state.candidate_finding_promotions() == []
        assert not llm.requests and not tools.inner.invocations
