"""Plan 1d: one offline Run joins cooldown, restrained Worker, and v2 promotion.

The provider and target are deterministic doubles.  This proves the production
orchestration/storage wiring, not live provider availability or a vulnerability.
"""

from __future__ import annotations

import json

from redteam.app.bootstrap import build_central_graph
from redteam.app.cli.commands.report import _render_markdown
from redteam.app.web.graph_read import read_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.enums import TaskStatus
from redteam.core.ids import EvidenceId, RunId
from redteam.core.ports.llm import LLMProviderFailure
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.graph_recording import GraphRecordingHandler
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.state import RuntimeState
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_development import TARGET, TOOL, call, discover, inspect, turn
from tests.integration.test_hypothesis_planner import candidate
from tests.integration.test_observation_candidate_chain_06d import ChainLLM, _meaning
from tests.integration.test_probe_workers import drive, selection


class IntegratedRunModel(ChainLLM):
    """One 429, then discovery, one target call, one evidence read, and settle."""

    def __init__(self, decisions):
        super().__init__(decisions)
        self.native_provider_calls = 0
        self.successful_worker_turns = 0
        self.evidence_read_calls = 0

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.last_native_request = {
            "messages": list(messages), "tools": list(tools), "system": system,
        }
        await self._wait_response("complete_native", messages)
        self.native_provider_calls += 1
        if self.native_provider_calls == 1:
            raise LLMProviderFailure(
                kind="rate_limited", http_status=429, provider_code="fixture_rate_limit",
                retryable=True, retry_after=2,
            )

        self.successful_worker_turns += 1
        payload = worker_input(messages)
        if self.successful_worker_turns == 1:
            return discover(TOOL)
        if self.successful_worker_turns == 2:
            return turn(inspect("one-external-action"))

        evidence = list(payload["probe"]["original_evidence_ids"])
        if self.successful_worker_turns == 3:
            assert len(evidence) == 1
            self.evidence_read_calls += 1
            return turn(call("one-evidence-read", "evidence.read", {
                "evidence_id": evidence[0], "offset": 0, "limit": 500,
            }))

        assert self.successful_worker_turns == 4
        self._canned_structured = {
            "accepted": True,
            "reason": "The preserved original was independently checked for task completion",
            "claims": [{
                "kind": "observation", "description": "Original response observed",
                "evidence_ids": evidence,
            }],
            "criterion_results": [{
                "criterion_id": criterion_id(CompletionCriterion.model_validate(item)),
                "met": True, "evidence_ids": evidence, "reason": "Response observed",
            } for item in payload["active_task"]["completion_criteria"]],
        }
        return turn(call("settle-task", "task.settle", {
            "outcome": "success", "reason": "The bounded observation is complete",
            "evidence_ids": evidence,
        }))

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema["properties"]
        if "meaning" in properties:
            await self._wait_response("complete_structured", messages)
            return _meaning("success")
        if "questions" in properties:
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            payload = json.loads(body)
            await self._wait_response("complete_structured", messages)
            self.planner_inputs.append(payload)
            response = self.decisions.pop(0)
            return response(payload) if callable(response) else response
        if "tasks" in properties:
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            payload = json.loads(body)
            await self._wait_response("complete_structured", messages)
            condition = CompletionCriterion(kind="observation", description="Inspect response")
            return {"tasks": [{
                "task_key": "one-bounded-probe", "question_id": "q1",
                "objective": "Probe the new premise", "title": "One bounded probe",
                "role_id": "probe-worker", "target_node_ids": [TARGET],
                "completion_criterion_ids": [criterion_id(condition)],
                "probe_limits": {"max_actions": 2, "max_model_requests": 8},
            }]}
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


def _assert_reconciled(accounting):
    """Every full partition must add back to the canonical Run totals."""
    for dimension in ("by_stage", "by_owner"):
        groups = accounting[dimension].values()
        for field in ("spent", "reserved", "known_tokens", "unknown_usage",
                      "unknown_dispatch", "provider_failures"):
            assert sum(group[field] for group in groups) == accounting[field]
    outcome_total = sum(accounting["outcomes"].values()) + accounting["unclassified_outcome"]
    purpose_total = sum(accounting["purposes"].values()) + accounting["unclassified_purpose"]
    attempt_total = sum(accounting["attempts"].values()) + accounting["unclassified_attempt"]
    assert outcome_total == purpose_total == attempt_total == accounting["spent"]


async def test_one_run_recovers_records_reviews_promotes_and_reopens(tmp_path):
    opening = {
        "updates": [candidate()],
        "questions": [{
            "question_id": "q1", "objective": "Probe the new premise",
            "target_node_ids": [TARGET],
            "completion_criteria": [{
                "kind": "observation", "description": "Inspect response",
            }],
            "candidate_refs": ["local"],
        }],
        "termination": "continue",
        "reason": "Observe exactly one response before judging the candidate",
    }
    llm = IntegratedRunModel([opening])
    state_path = evidence_root = runtime_json = run_id = engagement_id = None
    first_report_json = None

    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        metered = session.deps.llm_client
        assert isinstance(metered, RunMeteredLLM)
        now = [100.0]
        delays = []
        metered.ledger.clock = lambda: now[0]

        async def advance_clock(delay):
            delays.append(delay)
            now[0] += delay

        metered.sleep = advance_clock
        planner = SpecialistPlanner(session.deps)
        task_id, = await planner.advance(session.runtime)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        assert current.revision == 1 and current.status == "open"

        outcome, = await drive(session)
        task = await session.deps.central_state.get_task(task_id)
        assert outcome.kind == "completed", outcome.error
        assert task.status is TaskStatus.COMPLETED
        assert llm.native_provider_calls == 5
        assert llm.successful_worker_turns == 4
        assert llm.evidence_read_calls == 1
        assert delays == [2.0]
        assert len(tools.inner.invocations) == 1
        assert tools.inner.invocations[0].tool_id == TOOL
        assert outcome.runtime.active_task_id == task.task_id
        worker_payloads = [
            worker_input(request["messages"]) for request in llm.requests
            if request["kind"] == "complete_native"
        ]
        assert {item["active_task"]["task_id"] for item in worker_payloads} == {task_id}
        assert len({item["active_task"]["ownership_version"] for item in worker_payloads}) == 1
        assert len(outcome.runtime.evidence_reads) == 1
        assert outcome.runtime.evidence_reads[0]["request"]["limit"] == 500

        records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
        source = next(
            item for item in records
            if item.metadata.get("mission_task_id") == task_id
            and isinstance(item.metadata.get("tool_result"), dict)
        )
        verifier = StructuredLLMAnalysisVerifier(
            metered, session.deps.evidence_store, service,
        )
        handler = GraphRecordingHandler(
            evidence_store=session.deps.evidence_store,
            graph_service=service,
            verifier=verifier,
        )
        with llm_request_scope(str(session.runtime.run_id), "independent_reviewer"):
            reviewed = await handler({"findings": [{
                "statement": "The preserved response reflects the candidate premise",
                "evidence_refs": [str(source.evidence_id)],
                "target_node_id": TARGET,
                "hypothesis_id": current.hypothesis_id,
            }]}, LocalToolContext(session.deps, session.runtime, budget=DecisionBudget()))
        receipt_id = reviewed.payload["findings"][0]["receipt_id"]
        receipt = await session.deps.evidence_store.get(EvidenceId(receipt_id))
        assert receipt is not None
        assert receipt.metadata["candidate_revision"] == 1
        assert receipt.metadata["hypothesis_outcome"] == "success"
        saved = await service.get_hypothesis(current.hypothesis_id)
        findings = await service.list_findings()
        promotions = await session.deps.central_state.candidate_finding_promotions()
        assert saved.revision == 2 and saved.status == "confirmed"
        assert len(findings) == 1 and findings[0].verified is True
        assert set(findings[0].evidence_ids) == {str(source.evidence_id), receipt_id}
        assert len(promotions) == 1 and promotions[0]["status"] == "promoted"
        assert promotions[0]["finding_id"] == findings[0].finding_id

        accounting = await metered.ledger.snapshot(str(session.runtime.run_id))
        _assert_reconciled(accounting)
        task_accounting = accounting["by_task"][task_id]
        worker = accounting["by_owner"][outcome.runtime.work_owner]
        assert worker["spent"] == 5
        assert worker["known_tokens"] == 40 and worker["unknown_usage"] == 1
        assert worker["provider_failures"] == 1
        assert worker["attempts"]["transport_retry"] == 1
        assert worker["external_tool_decisions"] == 1
        assert worker["local_reads"] == 1
        assert worker["settlements"] == 1
        assert task_accounting["spent"] == 6  # Worker plus its independent completion review.
        assert task_accounting["unknown_usage"] == 2
        assert accounting["provider_cooldown"]["state"] == "open"
        assert accounting["provider_cooldown"]["consecutive"] == 0
        assert accounting["spent"] == 9
        assert accounting["known_tokens"] == 40
        assert accounting["unknown_usage"] == 5
        assert accounting["repairs"] == 0
        assert accounting["usage"]["total_tokens"] == {"known": 40, "unknown": 5}
        for field in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
        ):
            assert accounting["usage"][field] == {"known": 0, "unknown": 9}
        assert accounting["by_stage"]["hypothesis_planning"]["spent"] == 1
        assert accounting["by_stage"]["task_atomization"]["spent"] == 1
        assert accounting["by_stage"]["native_action"]["spent"] == 5
        assert accounting["by_stage"]["task_completion_review"]["spent"] == 1
        assert accounting["by_stage"]["analysis_claim_verify"]["spent"] == 1

        report = await ReportingAgent().project(
            session.runtime, graph=service, evidence_store=session.deps.evidence_store,
            work_store=None, task_store=session.deps.central_state,
            central_state=session.deps.central_state,
        )
        assert report.finding_count == 1 and report.findings[0].verified
        assert report.candidates[0].status == "confirmed"
        assert report.candidate_finding_promotions == promotions
        assert report.actions_executed == 1
        assert report.model_requests_used == 9
        assert report.model_tokens_used == 40
        assert report.model_usage_unknown == 5
        assert report.ready_for_release is False
        task_usage, = report.task_request_effectiveness
        assert task_usage["model_requests"] == 6
        assert task_usage["external_actions"] == 1
        assert task_usage["local_reads"] == 1
        rendered = _render_markdown(report)
        for marker in (
            "Provider cooldown: state=open", "transport retries=1", "provider failures=1",
            "status=confirmed", "status=promoted", "Findings: 1",
        ):
            assert marker in rendered

        web = await read_graph(session.deps.central_state.path, str(session.runtime.run_id))
        assert web["available"] is True
        assert web["origin"]["findings"] == web["origin"]["verified_findings"] == 1
        assert web["candidates"][0]["finding_promotion"]["status"] == "promoted"

        state_path = session.deps.central_state.path
        evidence_root = session.deps.evidence_store._root
        runtime_json = session.runtime.model_dump_json()
        run_id = str(session.runtime.run_id)
        engagement_id = str(session.runtime.engagement_id)
        first_report_json = report.model_dump(mode="json")

    assert state_path is not None and evidence_root is not None
    assert runtime_json is not None and run_id is not None and engagement_id is not None
    reopened = CentralStateStore(state_path, run_id=run_id, engagement_id=engagement_id)
    await reopened.setup()
    reopened_graph = AttackGraphService(build_central_graph(reopened))
    evidence = await FilesystemEvidenceStore.inspect_run(evidence_root, RunId(run_id))
    assert evidence is not None
    fresh_client = RunMeteredLLM(llm, reopened.request_ledger())
    reopened_accounting = await fresh_client.ledger.snapshot(run_id)
    _assert_reconciled(reopened_accounting)
    assert reopened_accounting["spent"] == 9
    assert len(await reopened_graph.list_findings()) == 1
    assert len(await reopened.candidate_finding_promotions()) == 1

    restarted_runtime = RuntimeState.model_validate_json(runtime_json)
    restarted_report = await ReportingAgent().project(
        restarted_runtime, graph=reopened_graph, evidence_store=evidence,
        work_store=None, task_store=reopened, central_state=reopened,
    )
    assert restarted_report.model_dump(mode="json") == first_report_json
    restarted_web = await read_graph(state_path, run_id)
    assert restarted_web["origin"]["findings"] == 1
    assert restarted_web["origin"]["verified_findings"] == 1
