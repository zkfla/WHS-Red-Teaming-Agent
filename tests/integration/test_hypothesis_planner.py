"""5c: real central planner/worker loops with explicit model/provider doubles only."""

import asyncio
import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus, TaskStatus, Verdict
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.analysis import VerificationAssessment, append_posthoc_claim_receipt
from redteam.orchestration.central_results import commit_result, result_for
from redteam.orchestration.central_runtime import ready_workers, runtime_token
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.hypothesis_planner import PLANNING_INVALIDATED
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_probe_workers import drive, register, selection


def probe(key="observe", *, candidate=None, revision=1, source=None):
    return {
        "task_key": key, "title": "Short observation", "objective": "Observe one response",
        "role_id": "probe-worker", "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
        "hypothesis_revisions": {candidate: revision} if candidate else {},
        "probe_limits": {"max_actions": 1, "max_model_requests": 4},
        **({"source_task_id": source["task_id"], "source_result_id": source["result"]["result_id"],
            "evidence_ids": source["result"]["evidence_ids"]} if source else {}),
    }


def candidate(key="local", *, current=None, status="open", evidence=(), review=None):
    return {"expected_revision": current["revision"] if current else None,
            "review_evidence_id": review, "hypothesis": {
                **(current or {}), "contract_version": 2,
                "hypothesis_id": current["hypothesis_id"] if current else key,
                "description": "Access may be unguarded", "target_node_ids": [TARGET],
                "reason": "Assess the preserved response", "status": status,
                "supporting_evidence_ids": list(evidence) if status != "rejected" else [],
                "refuting_evidence_ids": list(evidence) if status == "rejected" else [],
            }}


def held(reason="Await prerequisites"):
    return {"reason": reason, "termination": "held",
            "resume_conditions": ["An authorized prerequisite or original observation arrives"]}


class PlannerDouble(FakeLLMClient):
    def __init__(self, decisions):
        super().__init__()
        self.decisions = list(decisions)
        self.planner_inputs = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "updates" not in response_schema["properties"]:
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, system=system, **kwargs,
            )
        # The original message remains fixed while another worker publishes a result.
        body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
        payload = json.loads(body)
        await self._wait_response("complete_structured", messages)
        self.planner_inputs.append(payload)
        response = self.decisions.pop(0) if self.decisions else held()
        return response(payload) if callable(response) else response


@pytest.mark.parametrize("verdict", ["success", "refuted"])
async def test_candidate_probe_partial_followup_and_reviewed_verdict(tmp_path, verdict):
    first = {"updates": [candidate()], "tasks": [probe(candidate="local")],
             "reason": "Observe the candidate's smallest uncertainty"}

    def followup(payload):
        current, = payload["candidates"]
        source, = payload["investigations"]
        assert source["result"]["outcome"] == "inconclusive"
        return {"updates": [candidate(current=current,
                                      evidence=source["result"]["evidence_ids"])],
                "tasks": [probe("followup", candidate=current["hypothesis_id"],
                                revision=current["revision"] + 1, source=source)],
                "reason": "The same originals support a different verification purpose"}

    llm = PlannerDouble([first, followup])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        first_id, = await planner.advance(session.runtime)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        assert current.hypothesis_id != "local" and current.revision == 1
        llm.native_responses_by_task[first_id] = [discover(TOOL), turn(inspect("first"))]
        await drive(session)
        second_id, = await planner.advance(session.runtime)
        second = await session.deps.central_state.get_task(second_id)
        assert second.source_task_id == first_id and second.source_question_id is None
        assert second.hypothesis_revisions == {current.hypothesis_id: 2}
        llm.native_responses_by_task[second_id] = [discover(TOOL), turn(inspect("second"))]
        await drive(session)
        current = await service.get_hypothesis(current.hypothesis_id)
        sources = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
        source = next(item for item in sources if item.metadata.get("mission_task_id") == second_id
                      and isinstance(item.metadata.get("tool_result"), dict))
        receipt = await append_posthoc_claim_receipt(
            evidence_store=session.deps.evidence_store, run_id=session.runtime.run_id,
            engagement_id=session.runtime.engagement_id,
            claim={"statement": current.description}, evidence_ids=[str(source.evidence_id)],
            assessment=VerificationAssessment(
                verdict=Verdict.PASS, reviewer_id="fixture-independent-review",
                reason="Synthetic interpretation of preserved original", outcome=verdict,
                hypothesis_outcome=verdict, source_refs=[str(source.evidence_id)],
                claim_supported=verdict == "success",
            ),
            hypothesis_id=current.hypothesis_id, action_id=str(source.action_id),
            invocation_id=str(source.invocation_id), target_node_id=TARGET, candidate=current,
        )
        llm.decisions.append({"updates": [candidate(
            current=current.model_dump(mode="json"),
            status="confirmed" if verdict == "success" else "rejected",
            evidence=[str(source.evidence_id)], review=str(receipt.evidence_id),
        )], "reason": "The bound independent review supports this limited candidate verdict"})
        assert await planner.advance(session.runtime) == []
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.revision == 3 and saved.status == (
            "confirmed" if verdict == "success" else "rejected"
        )
        assert await service.list_findings() == []
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
        assert len(llm.planner_inputs) == 3
        assert len(llm.requests) == 7  # Three planner judgments plus two native requests per probe.


async def test_initial_observation_and_same_event_redelivery_are_distinct(tmp_path):
    llm = PlannerDouble([{"tasks": [probe()], "reason": "No candidate yet; inspect one response"}])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        identity, = await planner.advance(session.runtime)
        assert await planner.advance(session.runtime) == []
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.planner_inputs) == 1
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("initial"))]
        await drive(session)
        llm.decisions.append(lambda p: {
            "updates": [candidate()], "tasks": [probe(
                "candidate-probe", candidate="local", source=p["investigations"][0],
            )], "reason": "A scoped candidate now follows the original observation",
        })
        next_id, = await planner.advance(session.runtime)
        assert next_id != identity and len(llm.planner_inputs) == 2


async def test_same_result_id_updates_and_active_peer_are_visible_to_planner(tmp_path):
    llm = PlannerDouble([held(), held()])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identities = [await register(session, key, actions=2) for key in ("source", "peer")]
        workers = await ready_workers(session.deps, session.runtime)
        source = next(worker for worker in workers if str(worker.active_task_id) == identities[0])
        state = session.deps.central_state
        for index in range(2):
            task = await state.get_task(identities[0])
            result = result_for(task, outcome="inconclusive", summary="Observation update",
                                evidence_ids=[], observations=[f"synthetic observation {index}"])
            await commit_result(session.deps, source, task, result,
                                await runtime_token(session.deps, source), final=False)
            assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        snapshots = llm.planner_inputs
        assert len(snapshots) == 2
        sources = [next(task for task in item["investigations"] if task["task_id"] == identities[0])
                   for item in snapshots]
        assert sources[0]["result"]["result_id"] == sources[1]["result"]["result_id"]
        assert len(sources[1]["result"]["observations"]) == 2
        for identity in identities:
            assert (await state.get_task(identity)).status is TaskStatus.RUNNING


@pytest.mark.parametrize("related", [True, False])
async def test_planner_snapshot_rejects_related_updates_only(tmp_path, related):
    llm = PlannerDouble([held()])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        source_id = await register(session, "source", updates=[candidate()],
                                   hypotheses={"local": 1})
        current = (await AttackGraphService(session.deps.graph_store).list_hypotheses())[0]
        llm.decisions = [{"updates": [candidate(current=current.model_dump(mode="json"))],
                          "reason": "Review one captured candidate"}]
        llm.request_release.clear()
        running = asyncio.create_task(SpecialistPlanner(session.deps).advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            if related:
                await register(session, "changed", updates=[candidate(
                    current=current.model_dump(mode="json"), status="held",
                )], hypotheses={"local": 2})
            else:
                await register(session, "sibling")
        finally:
            llm.request_release.set()
        assert await running == []
        saved = await AttackGraphService(session.deps.graph_store).get_hypothesis("local")
        assert saved.status == ("held" if related else "open")
        assert saved.revision == 2
        invalidated = await session.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        )
        assert bool(invalidated) is related
        assert (await session.deps.central_state.get_task(source_id)).status is TaskStatus.READY


async def test_explicit_profile_runs_candidate_feedback_loop_without_atomizer(tmp_path):
    llm = PlannerDouble([
        {"updates": [candidate()], "tasks": [probe(candidate="local")], "reason": "Short probe"},
        held("The observation is inconclusive; an authorized prerequisite is missing"),
    ])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        async def script(envelope):
            identity = envelope.payload["task_id"]
            llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("original"))]

        from redteam.core.contracts.events import EventType

        await session.deps.event_bus.subscribe(EventType.TASK_CREATED, script)
        final = await asyncio.wait_for(session.run(settle=False), 5)
        assert final.actions_executed == 1 and len(llm.planner_inputs) == 2
        assert len(tools.inner.invocations) == 1
        assert len(llm.requests) == 4  # Two judgments and two native requests; no atomizer.
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE


@pytest.mark.parametrize("changed", ["source", "peer"])
async def test_result_arriving_during_planning_is_consumed_in_a_later_pass(tmp_path, changed):
    llm = PlannerDouble([])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        ids = {"source": await register(session, "source", hypotheses={"local": 1},
                                       updates=[candidate()]),
               "peer": await register(session, "peer")}
        workers = await ready_workers(session.deps, session.runtime)
        worker = next(item for item in workers if str(item.active_task_id) == ids[changed])
        state = session.deps.central_state
        current, = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        llm.decisions = [{"updates": [candidate(current=current.model_dump(mode="json"))],
                          "reviewed_task_ids": [ids["source"]], "reason": "Review source context"},
                         held("Read the newly committed observation")]
        planner = SpecialistPlanner(session.deps)
        llm.request_release.clear()
        running = asyncio.create_task(planner.advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            task = await state.get_task(ids[changed])
            result = result_for(task, outcome="inconclusive", summary="Committed while planning",
                                evidence_ids=[], observations=["Synthetic intermediate update"])
            await commit_result(session.deps, worker, task, result,
                                await runtime_token(session.deps, worker), final=False)
        finally:
            llm.request_release.set()
        await running
        assert await planner.advance(session.runtime) == []
        assert len(llm.planner_inputs) == 2
        first = next(item for item in llm.planner_inputs[0]["investigations"]
                     if item["task_id"] == ids[changed])
        second = next(item for item in llm.planner_inputs[1]["investigations"]
                      if item["task_id"] == ids[changed])
        assert first["result"] is None and second["result"]["observations"]
        invalidated = await state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        )
        assert bool(invalidated) is (changed == "source")
        assert await planner.advance(session.runtime) == []
        assert len(llm.planner_inputs) == 2


@pytest.mark.parametrize("pending", ["approval", "unknown"])
async def test_candidate_loop_never_refutes_pending_or_unknown_execution(tmp_path, pending):
    from tests.integration.test_central_completion import OriginalProtocolDouble
    from tests.integration.test_central_development import gateway

    tools = OriginalProtocolDouble(gateway(
        risk="high" if pending == "approval" else "low", unknown=pending == "unknown",
    ))
    llm = PlannerDouble([
        {"updates": [candidate()], "tasks": [probe(candidate="local")], "reason": "Probe"},
        held("The original is unresolved; this is not refutation"),
    ])
    async with selection(tmp_path, llm=llm, tools=tools) as (session, llm, tools):
        from redteam.core.contracts.events import EventType

        async def script(envelope):
            llm.native_responses_by_task[envelope.payload["task_id"]] = [
                discover(TOOL), turn(inspect("protected-original")),
            ]

        await session.deps.event_bus.subscribe(EventType.TASK_CREATED, script)
        await asyncio.wait_for(session.run(settle=False), 5)
        task, = await session.deps.central_state.list_tasks()
        saved, = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert task.status is TaskStatus.BLOCKED and task.result.outcome == "blocked"
        assert saved.status == "open" and saved.revision == 1
        assert len(tools.inner.invocations) == (1 if pending == "unknown" else 0)


async def test_invalid_followup_is_repaired_within_one_shared_planner_budget(tmp_path):
    invalid = {"tasks": [{**probe(), "source_result_id": "invented-result"}],
               "reason": "Invalid source proposal"}
    llm = PlannerDouble([invalid, held("No source result exists; await authorized observations")])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.requests) == 2
        assert "probe_followup_requires_original_task_result_pair" in (
            llm.requests[1]["messages"][-1]["content"]
        )
        assert await session.deps.central_state.list_tasks() == []
        assert (await session.status())["usage"]["spent"] == 2


async def test_partial_gap_cannot_supply_a_candidate_verdict_receipt(tmp_path):
    llm = PlannerDouble([{"updates": [candidate()], "tasks": [probe(candidate="local")],
                         "reason": "Observe before making any verdict"}])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        identity, = await planner.advance(session.runtime)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("partial"))]
        await drive(session)
        task = await session.deps.central_state.get_task(identity)
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        llm.decisions.append({"updates": [candidate(
            current=current.model_dump(mode="json"), status="rejected",
            evidence=task.result.evidence_ids, review=task.result.evidence_ids[-1],
        )], "reason": "Attempt to use an unresolved limitation as proof"})
        with pytest.raises(DecisionFailure) as rejected:
            await planner.advance(session.runtime)
        assert rejected.value.diagnostic.code == "hypothesis_admission_rejected"
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "open" and saved.revision == 1
