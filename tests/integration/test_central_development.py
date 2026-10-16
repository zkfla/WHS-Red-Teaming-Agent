"""One real native/Control/SQLite worker, with explicit local doubles only."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.events import EventType
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import RESULT_UPDATED
from redteam.orchestration.central_runtime import ready
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_tasks import StrategyTasks, register_investigation_tasks
from redteam.orchestration.state import TerminationReason

PROFILE = Path(__file__).resolve().parents[2] / "config/agents/native-specialists-web.yaml"
HOST, TARGET, TOOL = "unit.local", "host:unit.local", "web.inspect"


def engagement(*, actions=6):
    return Engagement.model_validate(
        {
            "engagement_id": "source-eng",
            "name": "Synthetic investigation",
            "scope": {"allowed_hosts": [HOST]},
            "execution_budget": {"max_actions": actions},
            "rate_limit_policy": {"max_actions_per_minute": 100},
            "approval_policy": {"auto_approve": False},
            "created_at": datetime.now(timezone.utc),
        }
    )


def settings(*, requests=30):
    return OrchestrationSettings(
        global_concurrency=1,  # This file retains the original single-worker coverage.
        max_steps=12,
        max_no_progress_turns=1,
        specialist_profile_path=str(PROFILE),
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=requests),
    )


def gateway(*, risk="low", unknown=False, resource=False):
    tool = ToolDescriptor.model_validate(
        {
            "tool_id": TOOL,
            "server_id": "development",
            "tool_name": "inspect",
            "capability_tags": ["web"],
            "risk_level": risk,
            "health_status": "healthy",
            "target_types": ["host"],
            "argument_bindings": {"host": "host"},
            "resource_bindings": [{"namespace": "fixture-session", "argument": "account"}]
            if resource
            else [],
            "input_schema": {
                "type": "object",
                "required": ["host", "account"],
                "properties": {
                    "host": {"type": "string"},
                    "account": {"type": "string"},
                },
            },
        }
    )
    return DevelopmentToolGateway(
        [tool],
        [
            DevelopmentResponse(
                output={"status": 200, "account": "synthetic"},
                metadata={
                    "resource_effects": [],
                    **({"execution_uncertain": True} if unknown else {}),
                },
            )
            for _ in range(6)
        ],
    )


def proposal(**source):
    condition = {"kind": "semantic", "description": "Assess response using originals"}
    question = {
        "question_id": "q",
        "objective": "Investigate response",
        "target_node_ids": [TARGET],
        "completion_criteria": [condition],
    }
    return StrategyTasks.model_validate(
        {
            "questions": [question],
            "tasks": [
                {
                    "task_key": "task",
                    "question_id": "q",
                    "objective": question["objective"],
                    "title": "Response investigation",
                    "role_id": "web-executor",
                    "target_node_ids": [TARGET],
                    "completion_criteria": [condition],
                    **source,
                }
            ],
        }
    )


def call(identity, tool, arguments):
    return NativeToolCall(call_id=identity, name=exposed_tool_name(tool), arguments=arguments)


def turn(*calls):
    return NativeCompletion(tool_calls=tuple(calls), total_tokens=10)


def discover(*tools):
    return turn(
        *(call(f"detail-{i}", "tool.get_detail", {"tool_id": tool}) for i, tool in enumerate(tools))
    )


def inspect(identity, account="a", host=HOST):
    return call(identity, TOOL, {"host": host, "account": account})


async def register(selection, identity="registration-a", candidate=None):
    return await register_investigation_tasks(
        selection.deps.central_state,
        proposal_id=identity,
        proposal=candidate or proposal(),
        agents=selection.deps.agents,
        known_targets={TARGET},
        known_principals=set(),
        max_tasks=8,
        evidence_store=selection.deps.evidence_store,
    )


class ForbiddenLegacy:
    def __getattr__(self, name):
        raise AssertionError("central selection consulted legacy domain state")


async def test_planner_worker_intermediate_followup_and_atomic_originals():
    proposed = proposal()
    llm = FakeLLMClient(
        native_responses=[
            discover(TOOL, "task.publish_result", "task.settle", "evidence.read"),
            turn(inspect("inspect-a", "a"), inspect("inspect-b", "b")),
        ],
        structured_responses=[
            {
                "questions": [q.model_dump(mode="json") for q in proposed.questions],
                "reason": "Assess",
            },
            {"tasks": [t.model_dump(mode="json") for t in proposed.tasks]},
            *[
                {
                    "questions": [],
                    "reason": "Held for current worker results",
                    "termination": "held",
                    "resume_conditions": ["New worker result is committed"],
                }
                for _ in range(5)
            ],
        ],
    )
    tools = gateway()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings()
    ) as selection:
        await bind_run_metadata(selection.deps)
        state = selection.deps.central_state
        assert selection.deps.task_graph_store is None
        assert selection.deps.path_store is None
        assert selection.deps.work_store is None
        # Even explicit compatibility state is refused by central assembly.
        with pytest.raises(ValueError, match="central_runtime_cannot_mix_legacy_state_owners"):
            replace(selection.deps, task_graph_store=ForbiddenLegacy())
        published = []

        async def evidence_committed(envelope):
            evidence_id = envelope.payload["evidence_id"]
            assert await state.has_inspected_evidence(evidence_id)
            if len(tools.invocations) != 2:
                return
            if llm.native_responses:
                return
            # Feed the next fixture turn from the committed original ID, never a fake success.
            llm.native_responses.extend(
                [
                    turn(call("read-evidence", "evidence.read", {"evidence_id": evidence_id})),
                    turn(
                        call(
                            "publish-question",
                            "task.publish_result",
                            {
                                "result": {
                                    "result_id": "result-first",
                                    "outcome": "inconclusive",
                                    "summary": "Investigate another account independently",
                                    "contract_version": 1,
                                    "evidence_ids": [evidence_id],
                                    "questions": [
                                        {
                                            "question_id": "question-first",
                                            "question": "Check another account",
                                            "evidence_ids": [evidence_id],
                                        }
                                    ],
                                }
                            },
                        )
                    ),
                    turn(
                        call(
                            "unproved-success",
                            "task.settle",
                            {
                                "outcome": "success",
                                "reason": "A synthetic success is not live corroboration",
                                "evidence_ids": [evidence_id],
                            },
                        )
                    ),
                    turn(
                        call(
                            "settle-unresolved",
                            "task.settle",
                            {
                                "outcome": "inconclusive",
                                "reason": "Synthetic responses need live corroboration",
                                "evidence_ids": [evidence_id],
                            },
                        )
                    ),
                ]
            )

        async def intermediate(envelope):
            task = await state.get_task(envelope.payload["task_id"])
            assert task.status is TaskStatus.RUNNING
            assert task.result.questions[0].question_id == "question-first"
            followup = proposal(
                source_task_id=str(task.task_id),
                source_result_id=task.result.result_id,
                source_question_id="question-first",
            )
            published.append(str(task.task_id))
            # The existing planner must consume the original result and register
            # its source triple; the notification handler never creates a Task.
            llm.structured_responses[:0] = [
                {
                    "questions": [q.model_dump(mode="json") for q in followup.questions],
                    "reason": "Investigate the published follow-up question",
                },
                {"tasks": [t.model_dump(mode="json") for t in followup.tasks]},
            ]

        await selection.deps.event_bus.subscribe(EventType.EVIDENCE_CREATED, evidence_committed)
        await selection.deps.event_bus.subscribe(RESULT_UPDATED, intermediate)
        final = await selection.run()
        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD, (
            final.model_dump(mode="json")
        )
        assert len(tools.invocations) == 2
        assert tools.invocations[0].arguments != tools.invocations[1].arguments
        assert final.actions_executed == 2 and final.remaining_budget == 4
        current = [task for task in await state.list_tasks() if task.result is not None][0]
        assert current.status is TaskStatus.COMPLETED and current.result.outcome == "inconclusive"
        assert current.result.contract_version == 1
        assert published == [str(current.task_id)]
        followup = [
            task for task in await state.list_tasks() if task.source_task_id == current.task_id
        ][0]
        assert followup.source_result_id == current.result.result_id
        assert not followup.dependency_task_ids
        status = await selection.status()
        assert status["mission"]["status"] == MissionStatus.ACTIVE.value
        assert status["usage"]["spent"] >= final.model_requests_used
        executions = await state.list_executions(task_id=str(current.task_id))
        assert len(executions) == 2 and all(e.state is ExecutionState.SUCCEEDED for e in executions)
        assert (
            len({e.action_id for e in executions})
            == len({e.invocation_id for e in executions})
            == 2
        )
        for record in executions:
            assert await state.has_inspected_evidence(str(record.proposal_evidence_id))
            assert await state.has_inspected_evidence(str(record.result_evidence_id))
        with sqlite3.connect(state.path) as db:
            names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not names & {"paths", "work_items", "tasks", "questions", "claims"}
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM state_graph_mutations WHERE run_id=?",
                    (state.run_id,),
                ).fetchone()[0]
                > 0
            )
        assert (await state.inspect_evidence(selection.deps.evidence_store))["missing"] == []


async def test_registration_replay_and_new_requests_do_not_semantically_deduplicate():
    async with build_central_development(
        engagement(), llm=FakeLLMClient(), tools=gateway(), settings=settings()
    ) as selection:
        original = await register(selection)
        assert await register(selection) == original
        repeated = await register(selection, "new-request")
        assert repeated != original
        changed = proposal()
        changed.tasks[0].objective = "Changed purpose"
        with pytest.raises(StateConflict, match="registration_request_identity_conflict"):
            await register(selection, candidate=changed)
        changed = proposal()
        changed.tasks[0].task_key = "different-child"
        with pytest.raises(StateConflict, match="registration_request_identity_conflict"):
            await register(selection, candidate=changed)
        dag = proposal().model_dump(mode="json")
        dag["tasks"][0]["depends_on"] = ["task"]
        with pytest.raises(ValueError, match="Extra inputs"):
            StrategyTasks.model_validate(dag)


@pytest.mark.parametrize(
    "cause,expected_calls,reason",
    [
        ("budget", 1, TerminationReason.BUDGET_EXHAUSTED),
        ("stop", 1, TerminationReason.OPERATOR_STOPPED),
        ("pause", 1, TerminationReason.OPERATOR_PAUSED),
        ("unknown", 1, TerminationReason.RESOURCE_WAIT),
        ("approval", 0, TerminationReason.APPROVAL_PENDING),
        ("exclude", 1, TerminationReason.INVESTIGATION_HELD),
        ("scope", 1, TerminationReason.INVESTIGATION_HELD),
    ],
)
async def test_original_waits_and_actual_limits_block_new_calls(cause, expected_calls, reason):
    tools = gateway(risk="high" if cause == "approval" else "low", unknown=cause == "unknown")
    llm = FakeLLMClient(
        native_responses=[
            discover(TOOL),
            turn(
                inspect("first"),
                inspect("second", host="outside.local" if cause == "scope" else HOST),
            ),
        ]
    )
    async with build_central_development(
        engagement(actions=1 if cause == "budget" else 6), llm=llm, tools=tools, settings=settings()
    ) as selection:
        await register(selection)
        worker = await ready(selection.deps, selection.runtime)

        async def stopped(envelope):
            if cause not in {"stop", "pause", "exclude"}:
                return
            await selection.deps.central_state.append(
                IntentRecord(
                    run_id=worker.run_id,
                    correlation_id="operator-control",
                    intent=OperatorIntent(
                        intent_id="operator-control",
                        mission_id=worker.mission_id,
                        intent_type=cause,
                        instruction="Synthetic operator control",
                        created_at=datetime.now(timezone.utc),
                        parameters={"exclude_areas": ["web"]} if cause == "exclude" else {},
                        provenance=Provenance(source="test:operator"),
                    ),
                )
            )

        await selection.deps.event_bus.subscribe(EventType.EVIDENCE_CREATED, stopped)
        async for event in selection.driver.drive([worker]):
            assert event.kind == "completed", event.error
            final = event.runtime
        assert len(tools.invocations) == expected_calls
        assert final.termination_reason == reason
        execution = (await selection.deps.central_state.list_executions())[0]
        if cause == "unknown":
            assert execution.state is ExecutionState.UNKNOWN and execution.result_evidence_id
        if cause == "approval":
            assert execution.wait_reason == "approval" and execution.approval_ref
        if cause not in {"exclude", "scope"}:
            resumed = await selection.driver.runner_for(worker).resume_recovered(final)
            assert resumed == final and len(tools.invocations) == expected_calls


async def test_completed_only_continuation_retains_ids_and_shared_budgets():
    tools = gateway()
    llm = FakeLLMClient(
        native_responses=[
            discover(TOOL),
            turn(inspect("first")),
            NativeCompletion(text="Hold this investigation"),
        ]
    )
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings()
    ) as selection:
        await register(selection)
        worker = await ready(selection.deps, selection.runtime)
        async for event in selection.driver.drive([worker]):
            final = event.runtime
            assert event.kind == "completed", event.error
        first = tools.invocations[0]
        llm.native_responses.extend(
            [
                turn(inspect("different-input", "b")),
                NativeCompletion(text="Further evidence is needed"),
            ]
        )
        resumed = await selection.driver.runner_for(worker).resume_recovered(final)
        assert len(tools.invocations) == 2
        assert tools.invocations[0] == first
        assert resumed.actions_executed == 2 and resumed.remaining_budget == 4
        assert resumed.model_requests_used > final.model_requests_used
        assert resumed.step_count > final.step_count


async def test_live_provider_rejected_before_storage_or_provider_use():
    with pytest.raises(ValueError, match="exact_local_doubles"):
        async with build_central_development(
            engagement(), llm=object(), tools=gateway(), settings=settings()
        ):
            pytest.fail("live provider selection was accepted")


async def test_job_wait_preserves_original_execution_and_does_not_resubmit():
    jobs, tools = DevelopmentJobs(), gateway(resource=True)
    llm = FakeLLMClient(native_responses=[discover(TOOL), turn(inspect("job-call"))])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(), jobs=jobs
    ) as selection:
        await register(selection)
        worker = await ready(selection.deps, selection.runtime)
        async for result in selection.driver.drive([worker]):
            assert result.kind == "completed", result.error
            final = result.runtime
        assert final.termination_reason == TerminationReason.RESOURCE_WAIT
        execution = (await selection.deps.central_state.list_executions())[0]
        assert execution.wait_reason == "job" and execution.job_ref in jobs.submissions
        assert jobs.submissions[execution.job_ref].invocation_id == execution.invocation_id
        assert execution.state is ExecutionState.UNKNOWN
        assert execution.resource_keys and execution.resource_execution_pending
        assert len(jobs.submissions) == 1 and not tools.invocations
        assert await selection.driver.runner_for(worker).resume_recovered(final) == final
        assert len(jobs.submissions) == 1


async def test_shared_llm_limit_and_unprepared_goal_cannot_report_success():
    tools = gateway()
    llm = FakeLLMClient(native_responses=[discover(TOOL), turn(inspect("over-limit"))])
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(requests=1)
    ) as selection:
        await register(selection)
        worker = await ready(selection.deps, selection.runtime)
        async for result in selection.driver.drive([worker]):
            final = result.runtime
        assert final.termination_reason == TerminationReason.BUDGET_EXHAUSTED
        assert not tools.invocations
        usage = await selection.deps.central_state.request_ledger().snapshot(str(final.run_id))
        assert usage["spent"] == 1 and usage["reserved"] == 0

    proposed = proposal()
    planner_llm = FakeLLMClient(
        structured_responses=[
            {
                "questions": [q.model_dump(mode="json") for q in proposed.questions],
                "reason": "Assess",
            },
            {"tasks": [t.model_dump(mode="json") for t in proposed.tasks]},
        ]
    )
    async with build_central_development(
        engagement(), llm=planner_llm, tools=gateway(), settings=settings(requests=1)
    ) as selection:
        final = await selection.run()
        assert final.termination_reason == TerminationReason.BUDGET_EXHAUSTED
        assert not (await selection.status())["tasks"]
        assert (await selection.status())["usage"]["spent"] == 1

    llm = FakeLLMClient(
        native_responses=[
            discover("goal.settle"),
            turn(
                call(
                    "goal-proposal",
                    "goal.settle",
                    {"outcome": "success", "reason": "Unsupported claim"},
                )
            ),
        ]
    )
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings()
    ) as selection:
        await register(selection)
        worker = await ready(selection.deps, selection.runtime)
        async for result in selection.driver.drive([worker]):
            assert result.runtime.termination_reason != TerminationReason.GOAL_ACHIEVED
        assert (await selection.status())["mission"]["status"] == MissionStatus.ACTIVE.value
