"""Exact assessment draft -> review receipt -> candidate -> Finding detail flow."""

import json

import pytest

from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.report import _render_markdown
from redteam.app.web.graph_read import read_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse
from redteam.core.contracts.graph import HypothesisUpdate
from redteam.core.ids import EvidenceId
from redteam.core.ports.llm import NativeCompletion
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.findings import candidate_promotion_input
from redteam.orchestration.graph_recording import GRAPH_RECORD_TOOL_ID
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.tool_discovery import TOOL_GET_DETAIL_ID
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TARGET, TOOL, call, inspect, turn
from tests.integration.test_central_development import gateway as development_gateway
from tests.integration.test_hypothesis_planner import candidate, selection
from tests.integration.test_native_candidate_review_loop import (
    NativeReviewLLM,
    _task,
)
from tests.integration.test_probe_workers import drive

LONG_STATEMENT = "Observed retained field supports the candidate. " + "a" * 700 + (
    " The conclusion is limited to the stored subject and comparison."
)


def _draft(evidence_id: str, hypothesis_id: str) -> dict:
    return {
        "contract_version": 1,
        "draft_id": "draft-reviewed-detail",
        "name": "Stored subject boundary differs",
        "vulnclass": "Access boundary",
        "statement": LONG_STATEMENT,
        "evidence_refs": [evidence_id],
        "target_node_id": TARGET,
        "hypothesis_id": hypothesis_id,
        "judgment": "supports",
        "reason": "The selected stored field supports the scoped claim.",
        "severity": "high",
        "impact": "A protected response may be disclosed for the stored subject.",
        "expectation": {
            "statement": "The compared subject should not receive the protected response.",
            "source": "assumption",
        },
        "observations": [{
            "statement": "The retained status field is present.",
            "sources": [{
                "evidence_id": evidence_id,
                "raw_path": ["status"],
                "role": "support",
            }],
        }],
        "limitations": ["Only the retained subject and response are covered."],
        "alternative_explanations": ["The declared boundary may be incomplete."],
    }


class ReviewedDetailLLM(NativeReviewLLM):
    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema["properties"]
        if "meaning" in properties:
            self.meaning_calls += 1
            body = json.loads(messages[-1]["content"].split("\n\nPrevious attempt failed")[0])
            self.observed["review_payload"] = body
            return {
                "meaning": "The selected retained field supports the scoped claim.",
                "outcome": "success",
                "reason": "The full qualified claim is supported by the displayed source.",
                "claim_supported": True,
                "confidence": 0.9,
                "hypothesis_outcome": "success",
                "adopted_detail": {
                    "contract_version": 1,
                    "title": "Reviewed stored subject boundary differs",
                    "statement": LONG_STATEMENT,
                    "summary": "The stored comparison supports the scoped access-boundary claim.",
                    "vulnclass": "Access boundary",
                    "severity": "high",
                    "severity_reason": "The reviewed impact covers protected response disclosure.",
                    "impact": "A protected response may be disclosed for the stored subject.",
                    "limitations": ["Only the retained subject and response are covered."],
                },
            }
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


SQL_STATEMENT = (
    "Controlled SQL boolean conditions change the login result and bypass authentication."
)
SQL_LIMITATIONS = [
    "Session issuance, protected-resource access, and data extraction or modification were "
    "not established by these requests."
]


class SQLDifferentialReviewLLM(NativeReviewLLM):
    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "meaning" in response_schema["properties"]:
            self.meaning_calls += 1
            body = json.loads(messages[-1]["content"].split("\n\nPrevious attempt failed")[0])
            self.observed["review_payload"] = body
            return {
                "meaning": "The paired true/false conditions control the authentication result.",
                "outcome": "success",
                "reason": "The retained comparison supports the bounded authentication bypass.",
                "claim_supported": True,
                "confidence": 0.95,
                "hypothesis_outcome": "success",
                "adopted_detail": {
                    "contract_version": 1,
                    "title": "SQL condition controls authentication outcome",
                    "statement": SQL_STATEMENT,
                    "summary": "A SQL boolean condition controls the login result and bypasses "
                               "authentication.",
                    "vulnclass": "SQL injection",
                    "severity": "high",
                    "severity_reason": "The reviewed impact is limited to authentication bypass.",
                    "impact": (
                        "An unauthenticated request can obtain the administrator login result."
                    ),
                    "limitations": SQL_LIMITATIONS,
                },
            }
        return await super().complete_structured(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **kwargs,
        )


def _script(session, tools, hypothesis_id):
    async def script(step, llm):
        if step == 0:
            return turn(call("detail-target", TOOL_GET_DETAIL_ID, {"tool_id": TOOL}))
        if step == 1:
            return turn(inspect("observe"))
        if step == 2:
            return turn(call("detail-record", TOOL_GET_DETAIL_ID,
                             {"tool_id": GRAPH_RECORD_TOOL_ID}))
        if step == 3:
            records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
            source = next(item for item in records
                          if item.metadata.get("mission_task_id") == llm.worker
                          and isinstance(item.metadata.get("tool_result"), dict))
            llm.observed["source"] = source
            llm.observed["draft"] = _draft(str(source.evidence_id), hypothesis_id)
            llm.observed["draft"]["observations"][0]["sources"][0]["raw_path"] = llm.source_path
            return turn(call("record", GRAPH_RECORD_TOOL_ID, {
                "assessment_findings": [llm.observed["draft"]],
            }))
        if step == 4:
            return turn(call("settle", "task.settle", {
                "outcome": "inconclusive",
                "reason": "The observation is retained for Planner judgment.",
                "evidence_ids": [str(llm.observed["source"].evidence_id)],
                "assessment_drafts": [llm.observed["draft"]],
                "limitations": ["Candidate judgment remains separate."],
            }))
        return NativeCompletion(text="Settlement already submitted.")
    return script


@pytest.mark.parametrize("source_path", [["status"], [], ["output"]],
                         ids=["scalar", "structured-output", "prefixed-output"])
async def test_reviewed_detail_is_bound_to_stored_draft_and_reaches_reopened_report(
    tmp_path, source_path,
):
    opening = {"updates": [candidate()], "tasks": [_task()],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = ReviewedDetailLLM(outcome="success", decisions=[opening])
    llm.source_path = source_path
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        llm.worker, llm.script = task_id, _script(session, tools, current.hypothesis_id)

        event, = await drive(session)
        assert event.kind == "completed", event.error
        record_result = llm.results["record"]["payload"]["findings"][0]
        assert record_result["status"] == "pending_task_result"
        receipt = await session.deps.evidence_store.get(EvidenceId(record_result["receipt_id"]))
        assert receipt.metadata["review_contract_version"] == 4
        assert receipt.metadata["confidence"] == 0.9
        detail = receipt.metadata["reviewed_finding_detail"]
        assert detail["statement"] == LONG_STATEMENT
        assert detail["draft_binding"]["draft_id"] == "draft-reviewed-detail"
        payload = llm.observed["review_payload"]
        assert payload["claim"]["assessment_draft"]["statement"] == LONG_STATEMENT
        field = payload["excerpts"]["sources"][0]["windows"][0]
        assert field["raw_path"] == source_path and field["status"] == "ok"
        assert receipt.metadata["source_review"] == payload["excerpts"]
        assert receipt.metadata["source_review"]["sources"][0]["source_digest"] == (
            llm.observed["source"].digest
        )
        if source_path != ["status"]:
            assert field["field_kind"] == "structured" and field["text"] == ""
            assert field["output_fields"]["fields"]

        saved_task = await session.deps.central_state.get_task(task_id)
        assert saved_task.result.assessment_drafts[0].identity(saved_task.result.result_id) == {
            key: detail["draft_binding"][key]
            for key in ("result_id", "draft_id", "content_hash")
        }
        source = llm.observed["source"]
        update = candidate(
            current=current.model_dump(mode="json"), status="confirmed",
            evidence=[str(source.evidence_id)], review=str(receipt.evidence_id),
        )
        update_model = HypothesisUpdate.model_validate(update)
        changed_draft = saved_task.result.assessment_drafts[0].model_copy(
            update={"statement": LONG_STATEMENT + " Changed after review."}
        )
        changed_task = saved_task.model_copy(update={
            "result": saved_task.result.model_copy(
                update={"assessment_drafts": [changed_draft]}
            )
        })
        refused = candidate_promotion_input(
            update=update_model,
            committed=update_model.hypothesis.model_copy(
                update={"revision": update_model.expected_revision + 1}
            ),
            receipt=receipt,
            originals={str(source.evidence_id): source},
            run_id=str(session.runtime.run_id),
            engagement_id=str(session.runtime.engagement_id),
            task_results=[changed_task],
        )
        assert refused.request is None
        assert refused.refused_reason == "reviewed_assessment_draft_not_stored"
        applications = await session.deps.central_state.candidate_review_applications(
            task_id=task_id,
        )
        assert len(applications) == 1
        assert applications[0]["status"] == "promoted"
        saved_candidate = await service.get_hypothesis(current.hypothesis_id)
        assert saved_candidate.status == "confirmed" and saved_candidate.revision == 2
        finding, = await service.list_findings()
        assert finding.title == "Reviewed stored subject boundary differs"
        assert finding.severity == "high"
        assert finding.metadata["reviewed_finding_detail"]["draft_binding"] == detail[
            "draft_binding"
        ]

        _, _, report = await inspect_central_run(
            session.deps.central_state.path, str(session.runtime.run_id),
        )
        projected, = report.findings
        assert projected.reviewed_detail.statement == LONG_STATEMENT
        assert projected.reviewed_detail.impact == detail["impact"]
        assert projected.review_receipt_id == str(receipt.evidence_id)
        assert projected.original_evidence_ids == [str(source.evidence_id)]
        assert report.assessment_reviews[0].reviewed_detail.draft_binding == (
            projected.reviewed_detail.draft_binding
        )
        application, = report.candidate_review_applications
        assert application.review_receipt_id == str(receipt.evidence_id)
        assert application.status == "promoted"
        assert application.candidate_pre_verdict_revision == 1
        assert application.candidate_revision == 2
        assert report.candidates[0].review_applications == [application]
        markdown = _render_markdown(report)
        assert "Reviewed stored subject boundary differs" in markdown
        assert "The conclusion is limited" in markdown
        assert detail["impact"] in markdown
        assert "revision=1 -> 2" in markdown
        assert "independent semantic review plus code-owned structural binding gates" in markdown
        assert exposed_tool_name(GRAPH_RECORD_TOOL_ID) in llm.advertised[3]


async def test_pending_review_retries_after_task_result_was_committed(tmp_path, monkeypatch):
    """A crash window after Task storage converges from the explicit pending ledger."""
    opening = {"updates": [candidate()], "tasks": [_task()],
               "reason": "Observe the candidate's smallest uncertainty"}
    llm = ReviewedDetailLLM(outcome="success", decisions=[opening])
    llm.source_path = ["status"]
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        llm.worker, llm.script = task_id, _script(session, tools, current.hypothesis_id)

        async def defer_pending(*args, **kwargs):
            return ()

        with monkeypatch.context() as patch:
            patch.setattr(
                "redteam.orchestration.candidate_review_application."
                "prepare_pending_candidate_reviews",
                defer_pending,
            )
            event, = await drive(session)
        assert event.kind == "completed", event.error
        pending, = await session.deps.central_state.candidate_review_applications(
            task_id=task_id,
            status="pending_task_result",
        )
        assert pending["draft_id"] == "draft-reviewed-detail"
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        assert await service.list_findings() == []

        _, _, pending_report = await inspect_central_run(
            session.deps.central_state.path, str(session.runtime.run_id),
        )
        assert pending_report.candidate_review_applications[0].status == (
            "pending_task_result"
        )
        assert await session.deps.central_state.candidate_review_applications(
            task_id=task_id,
        ) == [pending]
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == 1
        assert await service.list_findings() == []

        # The ordinary resume/dispatch boundary retries only this explicitly
        # pending current-Run application; it never scans historical receipts.
        assert await ready_workers(session.deps, session.runtime) == []
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "confirmed" and saved.revision == 2
        assert len(await service.list_findings()) == 1
        replay = await session.deps.candidate_review_applicator.retry_pending(task_id=task_id)
        assert replay == ()


async def test_paired_sql_differential_promotes_only_reviewed_authentication_scope(tmp_path):
    """Offline protocol evidence for the motivating SQL differential; no live target call."""
    tools = OriginalProtocolDouble(development_gateway())
    tools.inner.responses = [
        DevelopmentResponse(output={
            "status": 401,
            "login": "failed",
            "rendered_sql": "SELECT * FROM users WHERE username='invalid'",
        }),
        DevelopmentResponse(output={
            "status": 200,
            "login": "administrator",
            "rendered_sql": "SELECT * FROM users WHERE username='' OR 1=1 --'",
        }),
        DevelopmentResponse(output={
            "status": 401,
            "login": "failed",
            "rendered_sql": "SELECT * FROM users WHERE username='' OR 1=0 --'",
        }),
    ]
    task = _task()
    task["probe_limits"] = {"max_actions": 3, "max_model_requests": 12}
    opening = {"updates": [candidate()], "tasks": [task],
               "reason": "Compare normal, true-condition, and false-condition login results"}
    llm = SQLDifferentialReviewLLM(outcome="success", decisions=[opening])

    async with selection(tmp_path, llm=llm, tools=tools) as (session, llm, _):
        service = AttackGraphService(session.deps.graph_store)
        task_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
        current, = await service.list_hypotheses()
        llm.worker = task_id

        async def script(step, model):
            if step == 0:
                return turn(call("detail-target", TOOL_GET_DETAIL_ID, {"tool_id": TOOL}))
            if step == 1:
                return turn(inspect("baseline", account="invalid"))
            if step == 2:
                return turn(inspect("true-condition", account="' OR 1=1 --"))
            if step == 3:
                return turn(inspect("false-condition", account="' OR 1=0 --"))
            if step == 4:
                return turn(call("detail-record", TOOL_GET_DETAIL_ID,
                                 {"tool_id": GRAPH_RECORD_TOOL_ID}))
            if step == 5:
                records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
                sources = [item for item in records
                           if item.metadata.get("mission_task_id") == task_id
                           and isinstance(item.metadata.get("tool_result"), dict)]
                assert len(sources) == 3
                model.observed["sources"] = sources
                evidence_ids = [str(item.evidence_id) for item in sources]
                draft = {
                    "contract_version": 1,
                    "draft_id": "draft-sql-differential",
                    "name": "SQL condition controls authentication outcome",
                    "vulnclass": "SQL injection",
                    "statement": SQL_STATEMENT,
                    "evidence_refs": evidence_ids,
                    "target_node_id": TARGET,
                    "hypothesis_id": current.hypothesis_id,
                    "judgment": "supports",
                    "reason": "The true and false conditions produce paired login outcomes.",
                    "severity": "high",
                    "impact": (
                        "An unauthenticated request can obtain the administrator login result."
                    ),
                    "expectation": {
                        "statement": "Boolean SQL conditions must not control authentication.",
                        "source": "assumption",
                    },
                    "observations": [{
                        "statement": "Retained request and response fields for the comparison.",
                        "sources": [{
                            "evidence_id": identity,
                            "raw_path": [],
                            "role": "support",
                        } for identity in evidence_ids],
                    }],
                    "limitations": SQL_LIMITATIONS,
                    "alternative_explanations": [],
                }
                model.observed["draft"] = draft
                return turn(call("record", GRAPH_RECORD_TOOL_ID, {
                    "assessment_findings": [draft],
                }))
            if step == 6:
                sources = model.observed["sources"]
                return turn(call("settle", "task.settle", {
                    "outcome": "inconclusive",
                    "reason": "Store the bounded reviewed comparison.",
                    "evidence_ids": [str(item.evidence_id) for item in sources],
                    "assessment_drafts": [model.observed["draft"]],
                    "limitations": SQL_LIMITATIONS,
                }))
            return NativeCompletion(text="Settlement already submitted.")

        llm.script = script
        event, = await drive(session)
        assert event.kind == "completed", event.error
        recorded = llm.results["record"]["payload"]["findings"][0]
        assert recorded["status"] == "pending_task_result"
        applications = await session.deps.central_state.candidate_review_applications(
            task_id=task_id,
        )
        assert applications[0]["status"] == "promoted"
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "confirmed" and saved.revision == 2
        finding, = await service.list_findings()
        detail = finding.metadata["reviewed_finding_detail"]
        assert finding.verified is True
        assert detail["statement"] == SQL_STATEMENT
        assert detail["limitations"] == SQL_LIMITATIONS
        assert finding.description == (
            "A SQL boolean condition controls the login result and bypasses authentication."
        )
        assert len(finding.evidence_ids) == 4  # three originals plus the final review receipt
        shown = llm.observed["review_payload"]["excerpts"]["sources"]
        assert len(shown) == 3
        rendered = json.dumps(shown)
        assert "OR 1=1" in rendered and "OR 1=0" in rendered

        web = await read_graph(
            session.deps.central_state.path, str(session.runtime.run_id),
        )
        candidate_view, = web["candidates"]
        application, = candidate_view["review_applications"]
        finding_view, = candidate_view["findings"]
        assert application["status"] == "promoted"
        assert application["candidate_pre_verdict_revision"] == 1
        assert application["candidate_revision"] == 2
        assert finding_view["review_receipt_id"] == application["review_receipt_id"]
        assert finding_view["reviewed_detail"]["statement"] == SQL_STATEMENT
        assert finding_view["reviewed_detail"]["limitations"] == SQL_LIMITATIONS
        assert len(finding_view["original_evidence_ids"]) == 3
        # The graph payload carries references and bounded adopted detail, not
        # the retained HTTP/SQL bodies themselves.
        assert "rendered_sql" not in json.dumps(candidate_view)
