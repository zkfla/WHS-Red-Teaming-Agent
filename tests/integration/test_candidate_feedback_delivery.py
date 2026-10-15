"""6d: default split profile and actual storage/admission with explicit offline doubles."""

import json

import pytest

from redteam.app.cli.commands.report import _render_markdown
from redteam.app.web.graph_read import read_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.orchestration.local_tools import LocalToolDescriptor, LocalToolResult
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.task_completion_contract import check_task_conditions
from tests.integration.test_anonymous_completion import HTTP, provider, response
from tests.integration.test_central_development import HOST, TARGET, call, turn
from tests.integration.test_probe_workers import drive, selection
from tests.integration.test_v3_atomization import SplitDouble


def question(key, *, revisions=None, source=None, evidence=()):
    return {
        "question_id": key,
        "objective": "Narrow the anonymous response exposure premise",
        "target_node_ids": [TARGET],
        "principal_refs": ["anonymous"],
        "completion_criteria": [
            {
                "kind": "observation",
                "description": "Observe response",
                "principal_refs": ["anonymous"],
            }
        ],
        "hypothesis_revisions": revisions or {},
        "evidence_ids": list(evidence),
        **(
            {"source_task_id": source["task_id"], "source_result_id": source["result"]["result_id"]}
            if source
            else {}
        ),
    }


def atomize(payload):
    # Explicit fixture transformation; production still uses its independent model/validator.
    keys = (
        "objective",
        "target_node_ids",
        "principal_refs",
        "evidence_ids",
        "source_task_id",
        "source_result_id",
    )
    return {
        "tasks": [
            {
                "task_key": q["question_id"],
                "question_id": q["question_id"],
                "title": "Short premise observation",
                "role_id": "probe-worker",
                "probe_limits": {"max_actions": 2, "max_model_requests": 8},
                "completion_criterion_ids": [item["criterion_id"]
                                             for item in q["completion_criteria"]],
                "hypothesis_revisions": q["hypothesis_revisions"],
                **{key: q[key] for key in keys if key in q},
            }
            for q in payload["questions"]
        ]
    }


class FeedbackDouble(SplitDouble):
    def __init__(self, plans, *, defect=False):
        super().__init__(plans, [atomize] * 3)
        self.defect = defect
        self.worker_inputs = []
        self.worker_systems = []

    async def complete_native(self, messages, tools, system=None, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.worker_inputs.append(payload)
        self.worker_systems.append(system)
        evidence = payload["probe"]["original_evidence_ids"]
        diagnostics = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool" and "local_result_contract_invalid" in message["content"]
        ]
        if evidence and diagnostics:
            self.native_responses = [
                turn(
                    call(
                        "unresolved",
                        "task.settle",
                        {
                            "outcome": "inconclusive",
                            "reason": "Handler source correction is unavailable",
                            "evidence_ids": [*evidence, *diagnostics[-1]["evidence_ids"]],
                            "gaps": [
                                {
                                    "code": "assessment_failure",
                                    "description": "Runtime contract defect",
                                    "required_conditions": ["Correct the handler implementation"],
                                    "evidence_ids": diagnostics[-1]["evidence_ids"],
                                }
                            ],
                        },
                    )
                )
            ]
        elif evidence and self.defect:
            self.defect = False
            self.native_responses = [turn(call("defect", "fixture.contract", {}))]
        elif evidence:
            self._canned_structured = {
                "accepted": True,
                "reason": "Anonymous observation purpose met",
                "claims": [
                    {
                        "kind": "observation",
                        "description": "Observed anonymous response",
                        "evidence_ids": evidence,
                    }
                ],
                "criterion_results": [
                    {
                        "criterion_id": criterion_id(CompletionCriterion.model_validate(item)),
                        "met": True,
                        "evidence_ids": evidence,
                        "reason": "Observed response",
                    }
                    for item in payload["active_task"]["completion_criteria"]
                ],
            }
            self.native_responses = [
                turn(
                    call(
                        "return",
                        "task.settle",
                        {
                            "outcome": "success",
                            "reason": "Anonymous response observed",
                            "evidence_ids": evidence,
                        },
                    )
                )
            ]
        else:
            self.native_responses = [
                turn(
                    call(
                        "observe",
                        HTTP,
                        {
                            "host": HOST,
                            "path": "/",
                            "target_ref": HOST,
                        },
                    )
                )
            ]
        return await super().complete_native(messages, tools, system=system, **kwargs)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "criteria" in response_schema.get("properties", {}):
            payload = json.loads(messages[-1]["content"])
            self._canned_structured = {
                "accepted": False,
                "reason": "Authorization premise remains untested",
                "criteria": [
                    {"criterion": item, "met": False, "evidence_ids": [], "claims": []}
                    for item in payload.get("conditions", [])
                ],
            }
        return await super().complete_structured(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **kwargs,
        )


def source_task(payload):
    completed = [task for task in payload["investigations"] if task["result"]]
    linked = [task for task in completed if task["related_hypothesis_ids"]]
    return max(linked or completed, key=lambda task: task["updated_at"])


async def test_anonymous_observation_candidate_probe_same_candidate_feedback_and_report(tmp_path):
    def propose(payload):
        source = source_task(payload)
        assert payload["original_observations"]["records"]
        assert any(
            '"actual_session_ref": null' in view["text"]
            for view in payload["original_observations"]["records"]
        )
        evidence = source["result"]["evidence_ids"]
        return {
            "updates": [
                {
                    "expected_revision": None,
                    "hypothesis": {
                        "hypothesis_id": "premise",
                        "contract_version": 2,
                        "description": "Information may be exposed; authorization remains untested",
                        "reason": "Anonymous observation motivates a bounded premise check",
                        "target_node_ids": [TARGET],
                        "principal_refs": ["anonymous"],
                        "supporting_evidence_ids": evidence,
                        "metadata": {
                            "untested_premise": "Whether the response should require authorization"
                        },
                    },
                }
            ],
            "questions": [
                question("check", revisions={"premise": 1}, source=source, evidence=evidence)
            ],
            "reason": "Test the stated uncertainty",
            "termination": "continue",
        }

    def reconsider(payload):
        (candidate,) = payload["candidates"]
        assert candidate["status"] == "open" and candidate["revision"] == 1
        return {
            "updates": [
                {
                    "expected_revision": 1,
                    "hypothesis": {
                        **candidate,
                        "status": "held",
                        "reason": "Anonymous visibility does not prove a violation",
                    },
                }
            ],
            "questions": [],
            "reason": "Comparison premise needs authorized access",
            "termination": "held",
            "resume_conditions": ["An authorized comparison principal exists"],
            "reviewed_task_ids": [source_task(payload)["task_id"]],
            "reviewed_hypothesis_ids": [candidate["hypothesis_id"]],
        }

    llm = FeedbackDouble(
        [
            {
                "questions": [question("initial")],
                "reason": "Collect initial context",
                "termination": "continue",
            },
            propose,
            reconsider,
        ]
    )
    async with selection(tmp_path, tools=provider([response(), response()]), llm=llm) as (
        session,
        llm,
        tools,
    ):
        planner = SpecialistPlanner(session.deps)
        (first,) = await planner.advance(session.runtime)
        (first_return,) = await drive(session)
        task = await session.deps.central_state.get_task(first)
        assert (
            await check_task_conditions(
                task,
                task.result.evidence_ids,
                deps=session.deps,
                run_id=str(session.runtime.run_id),
            )
            == []
        )
        (second,) = await planner.advance(session.runtime)
        (second_return,) = await drive(session)
        assert first_return.kind == second_return.kind == "completed"
        following = await session.deps.central_state.get_task(second)
        assert (
            following.source_task_id == first
            and following.source_result_id == task.result.result_id
        )
        (original_candidate,) = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert await planner.advance(session.runtime) == []
        (updated,) = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert updated.hypothesis_id == original_candidate.hypothesis_id
        assert updated.revision == 2 and updated.status == "held"
        assert (
            len(llm.plan_inputs) == 3 and len(llm.atom_inputs) == 2
        )  # No atomizer for update-only held.
        assert not await AttackGraphService(session.deps.graph_store).list_findings()
        final = await session.settle(second_return.runtime)
        assert final.phase is AgentPhase.FINISHED
        assert (
            await session.deps.central_state.get_mission(final.mission_id)
        ).status is MissionStatus.ACTIVE
        report = await ReportingAgent().project(
            final,
            graph=AttackGraphService(session.deps.graph_store),
            evidence_store=session.deps.evidence_store,
            work_store=None,
            central_state=session.deps.central_state,
        )
        accounting = report.llm_request_accounting
        assert sum(item["spent"] for item in accounting["by_stage"].values()) == accounting["spent"]
        assert accounting["by_stage"]["task_atomization"]["spent"] == 2
        assert accounting["by_stage"]["task_completion_review"]["spent"] == 2
        assert "task_atomization" in _render_markdown(report) and report.finding_count == 0
        graph = await read_graph(session.deps.central_state.path, str(final.run_id))
        assert graph["planner"]["state"] == "held"
        assert all(worker["phase"] == "returned" for worker in graph["workers"])
        assert len(tools.inner.invocations) == 2


@pytest.mark.parametrize("independent", [False, True])
async def test_runtime_defect_preserves_originals_and_reaches_planner_independent_work_or_hold(
    tmp_path,
    independent,
):
    def next_plan(payload):
        source = source_task(payload)
        assert source["result"]["outcome"] == "inconclusive"
        assert any(
            "runtime_contract" in view["text"] for view in payload["runtime_records"]["records"]
        )
        assert payload["original_observations"]["records"]
        if independent:
            return {
                "questions": [question("independent", source=source)],
                "reason": "Narrow a separate response premise from the preserved observation",
                "termination": "continue",
            }
        return {
            "reason": "All useful next work depends on handler source correction",
            "questions": [],
            "termination": "held",
            "resume_conditions": ["Correct the handler implementation"],
        }

    llm = FeedbackDouble(
        [
            {
                "questions": [question("initial")],
                "reason": "Observe context",
                "termination": "continue",
            },
            next_plan,
        ],
        defect=True,
    )
    async with selection(tmp_path, tools=provider([response(), response()]), llm=llm) as (
        session,
        llm,
        tools,
    ):

        async def defective(arguments, context):
            return LocalToolResult(ok=False, runtime_outcome="task_completed")

        session.deps.local_tools.register(
            LocalToolDescriptor(
                tool_id="fixture.contract",
                description="Explicit defective test handler",
                input_schema={"type": "object"},
                initial_exposure=True,
            ),
            defective,
        )
        planner = SpecialistPlanner(session.deps)
        (first,) = await planner.advance(session.runtime)
        (returned,) = await drive(session)
        assert returned.kind == "completed", returned.error
        assert "runtime_contract" in llm.worker_systems[-1]
        assert "source correction" in llm.worker_systems[-1]
        identities = await planner.advance(session.runtime)
        assert bool(identities) is independent
        assert len(llm.atom_inputs) == (2 if independent else 1)
        assert not await AttackGraphService(session.deps.graph_store).list_hypotheses()
        if independent:
            task = await session.deps.central_state.get_task(identities[0])
            assert task.source_task_id == first and not task.related_hypothesis_ids
        else:
            graph = await read_graph(session.deps.central_state.path, str(session.runtime.run_id))
            assert graph["planner"]["state"] == "held"
            assert "source correction" in graph["planner"]["last_judgment"]["reason"]
        assert len(tools.inner.invocations) == 1  # Internal failure produced no target replay.
