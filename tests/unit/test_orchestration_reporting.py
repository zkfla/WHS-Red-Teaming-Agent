from datetime import datetime, timezone

import pytest

from redteam.attack_graph.hypotheses import prepare_hypothesis_update
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import CandidateHypothesis, Finding, HypothesisUpdate
from redteam.core.enums import AgentPhase, EvidenceSourceType
from redteam.core.ids import AgentId, EngagementId, EvidenceId, GraphNodeId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes.evidence_store import FakeEvidenceStore


async def test_reporting_refuses_a_nonterminal_runtime() -> None:
    with pytest.raises(ValueError, match="terminal runtime"):
        await ReportingAgent().project(
            RuntimeState(run_id=RunId("run"), engagement_id=EngagementId("eng")),
            graph=AttackGraphService(InMemoryAttackGraphStore()),
            evidence_store=FakeEvidenceStore(),
            work_store=InMemoryWorkStore(),
        )


async def test_empty_terminal_projection_is_releaseable_and_read_only() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
            termination_reason="no_actionable_plan",
        ),
        graph=graph,
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
    )

    assert projection.ready_for_release is True
    assert projection.finding_count == 0
    assert await graph.list_findings() == []


async def test_candidate_projection_is_not_a_finding_or_mission_verdict() -> None:
    from redteam.app.cli.commands.report import _render_markdown

    store = InMemoryAttackGraphStore()
    await store.add_node(prepare_hypothesis_update(
        None,
        HypothesisUpdate(hypothesis=CandidateHypothesis(
            hypothesis_id="candidate-a",
            description="Authorization may be missing",
            contract_version=2,
            target_node_ids=["host:example.invalid"],
            reason="Verify one response",
        )),
        now=datetime.now(timezone.utc),
        run_id="run",
    ))
    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"), engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED, termination_reason="investigation_held",
        ),
        graph=AttackGraphService(store),
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
    )

    assert projection.finding_count == 0 and projection.mission is None
    assert projection.candidates[0].model_dump() == {
        "hypothesis_id": "candidate-a",
        "description": "Authorization may be missing",
        "status": "open",
        "revision": 1,
        "confidence": 0.0,
        "reason": "Verify one response",
        "target_node_ids": ["host:example.invalid"],
        "supporting_evidence_ids": [],
        "refuting_evidence_ids": [],
        "source_hypothesis_ids": [],
        "probe_task_ids": [],
    }
    markdown = _render_markdown(projection)
    assert "Vulnerability candidates" in markdown
    assert "separate from Task completion" in markdown


async def test_markdown_projects_safe_provider_cooldown_state() -> None:
    from redteam.app.cli.commands.report import _render_markdown

    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"), engagement_id=EngagementId("eng"),
            phase=AgentPhase.FAILED, termination_reason="planning_error",
        ),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(), work_store=InMemoryWorkStore(),
    )
    projection.llm_request_accounting = {
        "policy": {"mode": "bounded", "max_requests": 10},
        "spent": 1, "reserved": 0, "known_tokens": 0, "unknown_usage": 1,
        "by_stage": {}, "usage": {},
        "provider_cooldown": {
            "version": 2, "state": "cooldown", "generation": 1,
            "consecutive": 1, "until": 123.0, "recovery_request_id": None,
            "last_failure": {
                "kind": "rate_limited", "provider_status": 429,
                "provider_code": "rate_limit", "retryable": True, "retry_after": 2.5,
            },
        },
    }

    markdown = _render_markdown(projection)
    assert "Provider cooldown: state=cooldown; consecutive=1; retry_at=123.0" in markdown
    assert "kind=rate_limited; status=429; code=rate_limit" in markdown


async def test_reporting_preserves_operational_diagnostic_in_json_and_markdown() -> None:
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.orchestration.diagnostics import operational_exception_diagnostic
    from redteam.orchestration.reporting import TerminalReportProjection
    from redteam.orchestration.runtime_observation import RuntimeObservation

    diagnostic = operational_exception_diagnostic(
        OSError("opaque-private-evidence-path"), stage="verify", code="verification_failed",
        summary="Evidence verification could not finish.",
    )
    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"), engagement_id=EngagementId("eng"),
            phase=AgentPhase.FAILED,
            recent_observations=[RuntimeObservation(
                kind="tool", summary="Evidence verification could not finish.",
                outcome="failed", step=1, diagnostic=diagnostic,
            )],
        ),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(), work_store=InMemoryWorkStore(),
    )
    restored = TerminalReportProjection.model_validate_json(projection.model_dump_json())
    assert restored.runtime_observations[0].diagnostic == diagnostic
    markdown = _render_markdown(restored)
    assert "verification_failed" in markdown
    assert "OSError" in markdown
    assert "opaque-private" not in markdown
    assert "opaque-private" not in restored.model_dump_json()


async def test_reporting_projects_ledger_derived_effectiveness() -> None:
    store = InMemoryWorkStore()
    now = datetime.now(timezone.utc)
    for index, status in enumerate((WorkStatus.SUCCEEDED, WorkStatus.FAILED)):
        item = WorkItem(
            work_id=f"work-{index}",
            run_id=RunId("run"),
            candidate_key=f"candidate-{index}",
            agent_id=AgentId("agent"),
            target_node_ids=[GraphNodeId("host:a.local")],
            priority=PriorityScore(total=1),
            status=status,
            created_at=now,
            updated_at=now,
        )
        await store.add(item)

    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
            actions_executed=2,
        ),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=store,
    )

    assert projection.effectiveness.successful_work_per_action == 0.5
    assert projection.effectiveness.dead_end_work_ratio == 0.5


async def test_reporting_excludes_findings_supported_only_by_another_run() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    await graph.record_finding(
        Finding(
            finding_id="find-current",
            title="current",
            description="current run",
            severity="medium",
            verified=True,
            evidence_ids=["ev-current", "ev-other"],
        )
    )
    await graph.record_finding(
        Finding(
            finding_id="find-other",
            title="other",
            description="other run",
            severity="medium",
            verified=True,
            evidence_ids=["ev-other"],
        )
    )
    evidence_store = FakeEvidenceStore()
    await evidence_store.append(
        EvidenceRecord(
            evidence_id=EvidenceId("ev-current"),
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="tool",
            content_type="application/json",
            digest="digest",
            created_at=datetime.now(timezone.utc),
        )
    )

    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
        ),
        graph=graph,
        evidence_store=evidence_store,
        work_store=InMemoryWorkStore(),
    )

    assert [item.finding_id for item in projection.findings] == ["find-current"]
    assert projection.findings[0].evidence_ids == ["ev-current"]


async def test_unresolved_store_detail_and_read_status_survive_saved_report(tmp_path):
    from types import SimpleNamespace

    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.orchestration.completion_proposal import (
        CompletionProposal,
        InvestigationGap,
        record_unresolved,
    )
    from redteam.orchestration.evidence_access import EvidenceReadRequest, read_for_runtime

    runtime = RuntimeState(
        run_id="run", engagement_id="eng", mission_id="mission",
        phase=AgentPhase.FINISHED, termination_reason="investigation_held",
        model_requests_used=5, model_tokens_used=19, model_usage_unknown=2,
    )
    store = FakeEvidenceStore()
    identity = await record_unresolved(
        evidence_store=store, runtime=runtime,
        proposal=CompletionProposal(
            outcome="inconclusive", reason="Need another account; token=private-value",
            gaps=[InvestigationGap(
                code="insufficient_evidence", description="Comparison remains unverified",
                unresolved_questions=["Does the second account see the same record?"],
                required_conditions=["Obtain authorized second account"],
                next_suggestions=["Compare authorized session results"],
            )],
        ), path=SimpleNamespace(path_id="path", version=2),
        step_id="step", task_id="task",
    )
    runtime.unresolved_evidence_ids = [identity]
    await store.append(EvidenceRecord(
        evidence_id="raw-evidence", run_id="run", engagement_id="eng",
        source_type=EvidenceSourceType.TOOL, source_id="tool",
        content_type="application/json", digest="digest",
        created_at=datetime.now(timezone.utc),
        metadata={"tool_result": {"output": {"body": "opaque-raw-not-for-report"}}},
    ))
    await read_for_runtime(store, runtime, EvidenceReadRequest(
        evidence_id="raw-evidence", offset=3, limit=8, query="opaque",
    ), delegation_id="delegation")
    await read_for_runtime(store, runtime, EvidenceReadRequest(evidence_id="missing"))
    # A foreign engagement's note must not supply detail to this report.
    note = await store.get(identity)
    await store.append(note.model_copy(update={
        "evidence_id": "foreign-note", "engagement_id": "other-engagement",
    }))
    runtime = RuntimeState.model_validate_json(runtime.model_dump_json())
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=store, work_store=InMemoryWorkStore(),
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored.run_outcome == "unresolved" and not restored.ready_for_release
    assert restored.unresolved_evidence_ids == [identity]
    assert [item.evidence_id for item in restored.unresolved_details] == [identity]
    detail = restored.unresolved_details[0]
    assert detail.reported_by == "model" and not detail.claim_supported
    assert (detail.mission_id, detail.task_id, detail.path_id, detail.path_step_id) == (
        "mission", "task", "path", "step",
    )
    assert detail.gaps[0].required_conditions == ["Obtain authorized second account"]
    assert [(entry.status, entry.delegation_id) for entry in restored.evidence_reads] == [
        ("ok", "delegation"), ("not_found", None),
    ]
    assert restored.evidence_reads[0].offset == 3 and restored.evidence_reads[0].query == "opaque"
    assert (restored.model_requests_used, restored.model_tokens_used,
            restored.model_usage_unknown) == (5, 19, 2)
    rendered = restored.model_dump_json() + _render_markdown(restored)
    assert "private-value" not in rendered and "opaque-raw-not-for-report" not in rendered
    assert "Obtain authorized second account" in rendered and "not_found" in rendered


async def test_recovered_planning_failure_history_remains_in_persisted_report(tmp_path):
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.orchestration.diagnostics import DecisionDiagnostic

    diagnostic = DecisionDiagnostic(
        code="llm_provider_rate_limited", stage="path_review", kind="transport",
        summary="Provider request failed", attempts=1, retryable=True,
        provider_status=429, provider_code="rate_limit", retry_after=2.5,
    )
    runtime = RuntimeState(
        run_id="run", engagement_id="eng", phase=AgentPhase.FINISHED,
        termination_reason="investigation_held", planning_diagnostic=None,
        planning_diagnostic_history=[diagnostic],
    )
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(), work_store=InMemoryWorkStore(),
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored.model_dump(mode="json").get("planning_diagnostic_history") == [
        diagnostic.model_dump(mode="json"),
    ]
    text = _render_markdown(restored)
    assert "rate_limit" in text and "429" in text and "2.5" in text


async def _settled_execution_fixture():
    from redteam.core.contracts.action import ActionProposal
    from redteam.core.contracts.completion import completion_contract_hash
    from redteam.core.contracts.execution import ExecutionState, TaskExecution
    from redteam.core.contracts.mission import MissionTask
    from redteam.core.contracts.tool import ToolInvocation, ToolResult
    from redteam.core.convert import execution_input_digest
    from redteam.core.tool_binding import execution_fingerprint
    from redteam.orchestration.agents.base import PlanStep
    from redteam.orchestration.execution_journal import append_execution_receipt

    now = datetime.now(timezone.utc)
    task = MissionTask(
        task_id="task", mission_id="mission", title="Check one response",
        objective="Inspect the authorized response", assigned_agent="web",
        created_at=now, updated_at=now, provenance={"source": "test"},
    )
    arguments = {"query": "fixture"}
    proposal = ActionProposal(
        action_id="action", run_id="run", engagement_id="eng", agent_id="web",
        instance_id="instance", binding_mode="selected_tool", tool_id="tool-any",
        target=None, arguments=arguments, reason="Synthetic unit-test proposal",
        created_at=now,
    )
    invocation = ToolInvocation(
        invocation_id="invocation", action_id="action", run_id="run",
        tool_id="tool-any", binding_mode="selected_tool", arguments=arguments,
        execution_context={
            "engagement_id": "eng", "run_id": "run", "agent_id": "web",
            "instance_id": "instance",
        },
        created_at=now,
    )
    step = PlanStep(
        step_id="step", tool_id="tool-any", binding_mode="selected_tool",
        execution_origin_version=1, mission_task_id="task",
        task_purpose_hash=completion_contract_hash(task),
        model_tool_call_id="call", reason="Synthetic unit-test step",
    )
    result = ToolResult(
        invocation_id="invocation", action_id="action", tool_id="tool-any",
        success=True, output={"fixture": True}, started_at=now, finished_at=now,
    )
    store = FakeEvidenceStore()
    receipt = await append_execution_receipt(
        store, invocation=invocation, proposal=proposal, step=step,
        work_id=None, result=result,
    )
    execution = TaskExecution(
        engagement_id="eng", run_id="run", task_id="task", instance_id="instance",
        ownership_version=1, action_id="action", invocation_id="invocation",
        model_tool_call_id="call", tool_id="tool-any",
        input_digest=execution_input_digest(invocation),
        proposal_evidence_id="proposal-evidence", approval_ref="approval",
        approval_fingerprint=execution_fingerprint("tool-any", None, arguments),
        state=ExecutionState.SUCCEEDED, result_evidence_id=receipt.evidence_id,
    )
    return execution, receipt


def _approval_wait_evidence(
    *, evidence_id="probe-yield", task_id="task", invocation_id="invocation",
    action_id=None,
):
    return EvidenceRecord(
        evidence_id=evidence_id, run_id="run", engagement_id="eng",
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.investigation_result", content_type="application/json",
        digest="d" * 64, created_at=datetime.now(timezone.utc),
        metadata={
            "record_kind": "investigation_unresolved", "task_id": task_id,
            "reported_by": "system", "outcome": "blocked",
            "reason": "Probe returned preserved observations: approval_pending",
            "diagnostic": {
                "code": "probe_yield", "stage": "probe_return", "kind": "validation",
                "summary": "Approval was pending at yield",
                "cause_scope": "policy_wait", "retryable": False,
                "trace": [{
                    "invocation_id": invocation_id, "state": "pending",
                    "wait_reason": "approval", "result_evidence_id": None,
                    **({"action_id": action_id} if action_id is not None else {}),
                }],
            },
            "gaps": [{"code": "unknown", "description": "Historical wait"}],
        },
    )


async def test_reporting_marks_only_receipt_validated_historical_approval_waits_settled():
    from redteam.orchestration.reporting import _project_unresolved_details

    execution, receipt = await _settled_execution_fixture()
    probe_wait = _approval_wait_evidence()
    observation = EvidenceRecord(
        evidence_id="mapped-observation", run_id="run", engagement_id="eng",
        source_type=EvidenceSourceType.TOOL, source_id="test.http_request",
        action_id="action", invocation_id="invocation", content_type="application/json",
        digest="e" * 64, created_at=datetime.now(timezone.utc),
        metadata={"http_observation": {"status_code": 404}, "mapping_status": "mapped"},
    )
    final_result = EvidenceRecord(
        evidence_id="final-blocked", run_id="run", engagement_id="eng",
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.investigation_result", content_type="application/json",
        digest="f" * 64, created_at=datetime.now(timezone.utc),
        metadata={
            "record_kind": "investigation_unresolved", "task_id": "task",
            "reported_by": "model", "outcome": "blocked",
            "reason": "The source remains unavailable for inspection.",
            "source_evidence_ids": [str(probe_wait.evidence_id), str(observation.evidence_id)],
            "gaps": [
                {
                    "code": "approval_pending", "description": "Original approval was pending",
                    "evidence_ids": [str(probe_wait.evidence_id)],
                },
                {
                    "code": "insufficient_evidence", "description": "404 supplied no script source",
                },
            ],
        },
    )

    details = _project_unresolved_details(
        [receipt, observation, probe_wait, final_result], executions=[execution],
        run_id="run", engagement_id="eng",
    )
    by_id = {item.evidence_id: item for item in details}
    projected_wait = by_id["probe-yield"]
    assert projected_wait.outcome == "blocked"
    assert projected_wait.diagnostic.trace[0]["state"] == "pending"
    assert projected_wait.diagnostic.trace[0]["wait_reason"] == "approval"
    resolution = projected_wait.historical_approval_waits[0]
    assert resolution.resolution == "settled" and resolution.receipt_validated
    assert resolution.result_evidence_id == str(receipt.evidence_id)
    assert resolution.action_id == "action" and resolution.current_state == "succeeded"

    projected_result = by_id["final-blocked"]
    assert projected_result.outcome == "blocked"
    approval_gap, evidence_gap = projected_result.gaps
    assert approval_gap.code == "approval_pending"
    assert approval_gap.approval_wait_resolution == "settled"
    assert approval_gap.historical_approval_waits[0].invocation_id == "invocation"
    assert evidence_gap.code == "insufficient_evidence"
    assert evidence_gap.description == "404 supplied no script source"
    assert evidence_gap.approval_wait_resolution is None


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("pending", "pending"),
        ("unknown", "unresolved"),
        ("missing_execution", "unresolved"),
        ("missing_receipt", "unresolved"),
        ("mismatched_receipt_action", "unresolved"),
        ("mismatched_trace_action", "unresolved"),
        ("invalid_receipt_digest", "unresolved"),
        ("mismatched_task", "unresolved"),
        ("mismatched_run", "unresolved"),
        ("duplicate_execution", "unresolved"),
        ("pending_with_receipt", "unresolved"),
    ],
)
async def test_reporting_keeps_unverified_approval_waits_unresolved(case, expected):
    from redteam.core.contracts.execution import ExecutionState
    from redteam.orchestration.reporting import _project_unresolved_details

    execution, receipt = await _settled_execution_fixture()
    inventory = [receipt]
    executions = [execution]
    wait_evidence = _approval_wait_evidence()
    if case == "pending":
        execution = execution.model_copy(update={
            "state": ExecutionState.PENDING, "wait_reason": "approval",
            "result_evidence_id": None,
        })
    elif case == "unknown":
        execution = execution.model_copy(update={
            "state": ExecutionState.UNKNOWN, "wait_reason": None,
            "result_evidence_id": None,
        })
    elif case == "missing_receipt":
        execution = execution.model_copy(update={"result_evidence_id": "missing-receipt"})
    elif case == "mismatched_receipt_action":
        inventory = [receipt.model_copy(update={"action_id": "other-action"})]
    elif case == "mismatched_trace_action":
        wait_evidence = _approval_wait_evidence(action_id="other-action")
    elif case == "invalid_receipt_digest":
        inventory = [receipt.model_copy(update={"digest": "0" * 64})]
    elif case == "mismatched_task":
        execution = execution.model_copy(update={"task_id": "other-task"})
    elif case == "mismatched_run":
        execution = execution.model_copy(update={"run_id": "other-run"})
    elif case == "duplicate_execution":
        duplicate = execution.model_copy(update={"state": ExecutionState.UNKNOWN})
        executions = [execution, duplicate]
    elif case == "pending_with_receipt":
        execution = execution.model_copy(update={
            "state": ExecutionState.PENDING, "wait_reason": "approval",
        })
    if case == "missing_execution":
        executions = []
    elif case != "duplicate_execution":
        executions = [execution]
    detail = _project_unresolved_details(
        [*inventory, wait_evidence], executions=executions,
        run_id="run", engagement_id="eng",
    )[0]
    resolution = detail.historical_approval_waits[0]
    assert resolution.resolution == expected
    assert resolution.receipt_validated is (expected == "settled")


async def test_reporting_preserves_a_sibling_approval_wait_while_projecting_original_settled():
    from redteam.core.contracts.execution import ExecutionState
    from redteam.orchestration.reporting import _project_unresolved_details

    execution, receipt = await _settled_execution_fixture()
    active_sibling = execution.model_copy(update={
        "action_id": "sibling-action", "invocation_id": "sibling-invocation",
        "state": ExecutionState.PENDING, "wait_reason": "approval",
        "result_evidence_id": None,
    })
    historical = _approval_wait_evidence()
    history_metadata = dict(historical.metadata)
    diagnostic = dict(history_metadata["diagnostic"])
    diagnostic["trace"] = [
        *diagnostic["trace"],
        {
            "invocation_id": "sibling-invocation", "state": "pending",
            "wait_reason": "approval", "result_evidence_id": None,
        },
    ]
    history_metadata["diagnostic"] = diagnostic
    historical = historical.model_copy(update={"metadata": history_metadata})

    detail = _project_unresolved_details(
        [receipt, historical], executions=[execution, active_sibling],
        run_id="run", engagement_id="eng",
    )[0]
    assert [wait.resolution for wait in detail.historical_approval_waits] == [
        "settled", "pending",
    ]
    assert detail.historical_approval_waits[0].invocation_id == "invocation"
    assert detail.historical_approval_waits[1].invocation_id == "sibling-invocation"
    assert detail.outcome == "blocked"
