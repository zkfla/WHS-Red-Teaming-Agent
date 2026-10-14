"""Explicit doubles: rejection -> native feedback -> original store -> Planner/report."""

import json

from redteam.app.cli.commands.report import _render_markdown
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import completion_rejection
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.evidence_access import EvidenceReader, locate_raw
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.semantic_completion import verify_single_origin
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.task_completion_contract import check_task_conditions
from tests.integration.test_central_development import TOOL, call, discover, inspect, turn
from tests.integration.test_hypothesis_planner import PlannerDouble, held, selection
from tests.integration.test_probe_workers import drive, register


class RejectedReview(PlannerDouble):
    def __init__(self):
        super().__init__([held()])
        self.delivered = None

    async def complete_native(self, messages, tools, system=None, **kwargs):
        payload = json.loads(messages[-1]["content"])
        task = payload["active_task"]
        originals = payload["probe"]["original_evidence_ids"]
        for message in messages:
            if message["role"] != "tool":
                continue
            feedback = json.loads(message["content"])
            if feedback.get("payload", {}).get("diagnostics"):
                self.delivered = feedback
        if originals:
            if self.delivered:
                arguments = {"outcome": "inconclusive", "reason": "Review support unproven",
                             "evidence_ids": [*originals, *self.delivered["evidence_ids"]]}
            else:
                arguments = {"outcome": "success", "reason": "Observed response",
                             "evidence_ids": originals}
                self._canned_structured = {
                    "accepted": False, "reason": "The delegated purpose remains unproven",
                    "claims": [], "criterion_results": [{
                        "criterion_id": criterion_id(CompletionCriterion.model_validate(item)),
                        "met": False, "evidence_ids": originals, "reason": "Insufficient support",
                    } for item in task["completion_criteria"]],
                }
            self.native_responses_by_task[task["task_id"]] = [
                turn(call("return", "task.settle", arguments)),
            ]
        return await FakeLLMClient.complete_native(self, messages, tools, system=system, **kwargs)


async def test_completion_rejection_survives_storage_read_planner_and_report(tmp_path):
    async with selection(tmp_path, llm=RejectedReview()) as (session, llm, tools):
        identity = await register(session, "diagnostic-delivery", actions=2, requests=8)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("observe"))]
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        assert llm.delivered is not None
        delivered = llm.delivered["payload"]["diagnostics"][0]
        task = await session.deps.central_state.get_task(identity)
        assert task.result.outcome == "inconclusive"
        assert delivered["code"] == "completion_review_rejected"
        assert delivered["cause_scope"] == "target_prerequisite" and not delivered["retryable"]
        assert delivered["criterion_ids"] == [criterion_id(task.completion_criteria[0])]
        assert delivered["evidence_ids"] and delivered["repair_condition"]
        diagnostic_id, = llm.delivered["evidence_ids"]
        stored = await session.deps.evidence_store.get(diagnostic_id)
        assert locate_raw(stored, str(session.runtime.run_id))[0] == "no_raw"
        view = await EvidenceReader(session.deps.evidence_store).read(
            diagnostic_id, str(session.runtime.run_id),
            engagement_id=str(session.runtime.engagement_id),
        )
        assert view["status"] == "ok" and not view["execution_original"]
        assert json.loads(view["text"])["result"]["payload"]["diagnostics"][0] == delivered
        _, errors = await verify_single_origin(
            evidence_store=session.deps.evidence_store, path_store=session.deps.path_store,
            run_id=str(session.runtime.run_id), engagement_id=str(session.runtime.engagement_id),
            mission_id=str(session.runtime.mission_id), evidence_id=diagnostic_id,
        )
        assert errors  # Displayable diagnostics cannot stand in for target execution.
        await SpecialistPlanner(session.deps).advance(session.runtime)
        inputs = llm.planner_inputs[-1]
        views = inputs["runtime_records"]["records"]
        assert any(item["evidence_id"] == diagnostic_id and delivered["code"] in item["text"]
                   for item in views)
        original = await session.deps.evidence_store.get(delivered["evidence_ids"][0])
        assert any(item["invocation_id"] == str(original.invocation_id)
                   and item["state"] == "succeeded" for item in inputs["execution_history"])
        report = await ReportingAgent().project(
            session.runtime, graph=AttackGraphService(session.deps.graph_store),
            evidence_store=session.deps.evidence_store, work_store=None,
            central_state=session.deps.central_state,
        )
        assert delivered["code"] in _render_markdown(report)
        assert report.finding_count == 0 and len(tools.inner.invocations) == 1
        # A real fixed-condition rejection keeps producer codes and exact criterion refs.
        scoped = task.model_copy(update={"principal_refs": ["anonymous"]})
        errors = await check_task_conditions(
            scoped, delivered["evidence_ids"], deps=session.deps,
            run_id=str(session.runtime.run_id),
        )
        assert errors and errors.diagnostics
        rejection = await completion_rejection(
            errors, CompletionProposal(outcome="success", evidence_ids=delivered["evidence_ids"]),
            scoped, session.deps, session.runtime,
        )
        assert [item["code"] for item in rejection.payload["diagnostics"]] == [
            item.code for item in errors.diagnostics
        ]
        assert all(item["criterion_ids"] == delivered["criterion_ids"]
                   and item["evidence_ids"] == delivered["evidence_ids"]
                   for item in rejection.payload["diagnostics"])
