"""Split-stage v3 planner/atomizer loop tests (async integration).

Drives SpecialistPlanner.advance on the split default profile
(config/agents/native-probes.yaml) via the public
tests.integration.test_probe_workers.selection harness, plus
ready_workers dispatch: two-ask success, update-only single request,
same-pass new-candidate forwarding, shared-budget repair, atomizer
failure with no partial commit, stop and related-drift discard between
the asks, unrelated-sibling tolerance, cursor idempotence, real storage
resume, and legacy single-stage compatibility.
"""

import inspect as stdlib_inspect
import json

import pytest

from redteam.app.central_run import open_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.enums import MissionStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import runtime_token
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.hypothesis_planner import PLANNING_INVALIDATED
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import (
    TARGET,
    TOOL,
    discover,
    gateway,
    inspect,
    turn,
)
from tests.integration.test_central_parallel import limits
from tests.integration.test_probe_workers import PROFILE, drive
from tests.integration.test_probe_workers import selection as probe_selection


def _condition_id(description, **kwargs):
    return criterion_id(CompletionCriterion(kind="observation", description=description, **kwargs))


class SplitDouble(FakeLLMClient):
    """Stage-1 plans (questions) then stage-2 atomizations (tasks)."""

    def __init__(self, plans, atoms):
        super().__init__()
        self.plans = list(plans)
        self.atoms = list(atoms)
        self.plan_inputs = []
        self.atom_inputs = []
        self.systems = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        props = response_schema.get("properties", {})
        if "questions" in props:
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            payload = json.loads(body)
            await self._wait_response("complete_structured", messages)
            self.plan_inputs.append(payload)
            self.systems.append(system)
            response = self.plans.pop(0) if self.plans else {
                "updates": [], "questions": [], "reason": "Await observations",
                "termination": "held", "resume_conditions": ["An observation arrives"]}
            result = response(payload) if callable(response) else response
            if stdlib_inspect.isawaitable(result):
                result = await result
            if (
                not payload.get("candidates")
                and isinstance(result, dict)
                and result.get("context_request") is None
                and not result.get("updates")
                and result.get("termination", "continue") == "continue"
                and result.get("questions")
            ):
                result = {**result, "updates": [{
                    "expected_revision": None,
                    "hypothesis": {
                        "contract_version": 2,
                        "hypothesis_id": "initial-local",
                        "description": "A target security premise remains untested",
                        "target_node_ids": [TARGET],
                        "reason": "Preserve the first scoped uncertainty for investigation",
                        "status": "open",
                    },
                }]}
            return result
        if "tasks" in props:
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            payload = json.loads(body)
            await self._wait_response("complete_structured", messages)
            self.atom_inputs.append(payload)
            self.systems.append(system)
            response = self.atoms.pop(0)
            result = response(payload) if callable(response) else response
            if stdlib_inspect.isawaitable(result):
                result = await result
            return result
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs)

def _plan_two_questions():
    return {"updates": [], "questions": [
        {"question_id": "q1", "objective": "Inspect the first response",
         "target_node_ids": [TARGET],
         "candidate_refs": [],
         "completion_criteria": [{"kind": "observation", "description": "Inspect first"}]},
        {"question_id": "q2", "objective": "Inspect the second response",
         "target_node_ids": [TARGET],
         "candidate_refs": [],
         "completion_criteria": [{"kind": "observation", "description": "Inspect second"}]}],
        "reason": "Two useful scoped observations", "termination": "continue"}

def _atom_two_tasks():
    return {"tasks": [
        {"task_key": "t1", "question_id": "q1", "objective": "Probe first",
         "title": "First probe", "role_id": "probe-worker",
         "target_node_ids": [TARGET],
         "completion_criterion_ids": [_condition_id("Inspect first")],
         "probe_limits": {"max_actions": 1, "max_model_requests": 4}},
        {"task_key": "t2", "question_id": "q2", "objective": "Probe second",
         "title": "Second probe", "role_id": "probe-worker",
         "target_node_ids": [TARGET],
         "completion_criterion_ids": [_condition_id("Inspect second")],
         "probe_limits": {"max_actions": 1, "max_model_requests": 4}}]}


async def test_split_loop_success_two_asks_and_dispatch(tmp_path):
    llm = SplitDouble([_plan_two_questions()], [_atom_two_tasks()])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        assert session.deps.specialist_policy.probe_atomization is True
        planner_role = session.deps.agent_prompts.binding("hypothesis_planning").role_id
        atom_role = session.deps.agent_prompts.binding("task_atomization").role_id
        assert planner_role != atom_role
        planner = SpecialistPlanner(session.deps)
        ids = await planner.advance(session.runtime)
        assert len(ids) == 2
        structured = [r for r in llm.requests if r["kind"] == "complete_structured"]
        assert len(structured) == 2
        assert len(llm.plan_inputs) == 1 and len(llm.atom_inputs) == 1
        assert llm.systems[0] != llm.systems[1]
        questions = llm.atom_inputs[0].get("questions", [])
        assert {q["question_id"] for q in questions} == {"q1", "q2"}
        assert (await session.status())["usage"]["spent"] == 2
        stored_pass = await session.deps.central_state.transaction(
            lambda tx: json.loads(tx.db.execute(
                "SELECT payload FROM state_requests WHERE run_id=? "
                "AND kind='hypothesis_planner_pass'",
                (str(session.runtime.run_id),),
            ).fetchone()[0]), write=False,
        )
        assert {q["question_id"] for q in stored_pass["questions"]} == {"q1", "q2"}
        assert stored_pass["atomization"]["coverage"] == {"q1": ["t1"], "q2": ["t2"]}
        for identity in ids:
            llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("probe"))]
        results = await drive(session)
        assert len(results) == 2 and all(r.kind == "completed" for r in results)

async def test_split_loop_update_only_makes_one_request(tmp_path):
    plan = {"updates": [{"expected_revision": None, "hypothesis": {
        "contract_version": 2, "hypothesis_id": "local",
        "description": "Access may be unguarded", "target_node_ids": [TARGET],
        "reason": "Assess the preserved response", "status": "open"}}],
        "questions": [], "reason": "Record premise", "termination": "replan"}
    llm = SplitDouble([plan], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert ids == []
        assert len(llm.atom_inputs) == 0
        assert len([r for r in llm.requests if r["kind"] == "complete_structured"]) == 1
        assert len(await AttackGraphService(session.deps.graph_store).list_hypotheses()) == 1

async def test_split_loop_new_candidate_forwarded_to_atomizer(tmp_path):
    llm = SplitDouble([_new_candidate_plan()], [_new_candidate_atom])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 1
        assert len(llm.plan_inputs) == 1 and len(llm.atom_inputs) == 1
        assert (await session.status())["usage"]["spent"] == 2
        (candidate,) = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert candidate.revision == 1
        task = await session.deps.central_state.get_task(ids[0])
        assert task.hypothesis_revisions == {candidate.hypothesis_id: 1}


async def test_split_loop_binds_unique_missing_verdict_reference_before_registration(tmp_path):
    plan = _new_candidate_plan()
    plan["questions"][0]["completion_criteria"] = [{
        "kind": "hypothesis_confirmed", "description": "Confirm the premise",
    }]

    def atomize(payload):
        (question,) = payload["questions"]
        (candidate_id,) = question["hypothesis_revisions"]
        criterion = question["completion_criteria"][0]
        assert criterion["hypothesis_id"] == candidate_id
        return {"tasks": [{
            "task_key": "t1", "question_id": "q1",
            "objective": "Probe the new premise", "title": "Verdict probe",
            "role_id": "probe-worker", "target_node_ids": [TARGET],
            "completion_criterion_ids": [
                criterion_id(CompletionCriterion.model_validate(criterion)),
            ],
            "probe_limits": {"max_actions": 1, "max_model_requests": 4},
        }]}

    llm = SplitDouble([plan], [atomize])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 1
        task = await session.deps.central_state.get_task(ids[0])
        (candidate_id,) = task.hypothesis_revisions
        assert task.completion_criteria[0].hypothesis_id == candidate_id
        (candidate,) = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert candidate.hypothesis_id == candidate_id and candidate.status == "open"


async def test_split_loop_ambiguous_missing_verdict_reference_commits_nothing(tmp_path):
    plan = {
        "updates": [
            {"expected_revision": None, "hypothesis": {
                "contract_version": 2, "hypothesis_id": "local-a",
                "description": "First premise", "target_node_ids": [TARGET],
                "reason": "Assess the first premise", "status": "open",
            }},
            {"expected_revision": None, "hypothesis": {
                "contract_version": 2, "hypothesis_id": "local-b",
                "description": "Second premise", "target_node_ids": [TARGET],
                "reason": "Assess the second premise", "status": "open",
            }},
        ],
        "questions": [{
            "question_id": "q1", "objective": "Confirm a candidate",
            "target_node_ids": [TARGET], "candidate_refs": ["local-a", "local-b"],
            "completion_criteria": [{
                "kind": "hypothesis_confirmed", "description": "Confirm the premise",
            }],
        }],
        "reason": "The criterion has multiple linked candidates",
        "termination": "continue",
    }
    # The second plan answer is a normal held repair after the field diagnostic.
    llm = SplitDouble([plan], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        await SpecialistPlanner(session.deps).advance(session.runtime)
        state = session.deps.central_state
        assert len(llm.plan_inputs) == 2
        assert await state.list_tasks() == []
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []

async def test_split_loop_updated_candidate_forwarded_to_atomizer(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate as _cand
    from tests.integration.test_probe_workers import register as _register
    llm = SplitDouble([], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        service = AttackGraphService(session.deps.graph_store)
        await _register(session, "seed", updates=[_cand()], hypotheses={"local": 1})
        (seed,) = await service.list_hypotheses()
        pid = seed.hypothesis_id
        assert seed.revision == 1
        llm.plans.append({
            "updates": [{"expected_revision": 1, "hypothesis": {
                **seed.model_dump(mode="json"),
                "description": "Access is guarded sometimes",
                "reason": "Refine the premise"}}],
            "questions": [{
                "question_id": "q1", "objective": "Re-probe the refined premise",
                "target_node_ids": [TARGET],
                "completion_criteria": [{"kind": "observation",
                                        "description": "Inspect refined",
                                        "hypothesis_id": pid}],
                "candidate_refs": [pid]}],
            "reason": "Refine and re-probe", "termination": "continue"})
        async def atom_updated(payload):
            (question,) = [item for item in payload["questions"]
                           if item["question_id"] == "q1"]
            assert question["hypothesis_revisions"] == {pid: 2}
            assert question["completion_criteria"][0]["hypothesis_id"] == pid
            assert question["objective"] == "Re-probe the refined premise"
            assert payload["candidate_revisions"][pid] == 2
            assert payload["issued_candidate_ids"] == {}
            assert payload["strategy"]["reason"] == "Refine and re-probe"
            (entry,) = payload["strategy"]["updates"]
            assert entry["hypothesis"]["hypothesis_id"] == pid
            assert entry["expected_revision"] == 1
            assert entry["proposed_revision"] == 2
            assert entry["hypothesis"]["description"] == "Access is guarded sometimes"
            (old,) = [item for item in payload["candidates"]
                      if item["hypothesis_id"] == pid]
            assert old["revision"] == 1
            return {"tasks": [{
                "task_key": "t1", "question_id": "q1",
                "objective": "Re-probe the refined premise", "title": "Refined probe",
                "role_id": "probe-worker", "target_node_ids": [TARGET],
                "completion_criterion_ids": [_condition_id("Inspect refined", hypothesis_id=pid)],
                "probe_limits": {"max_actions": 1, "max_model_requests": 4}}]}
        llm.atoms.append(atom_updated)
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 1
        assert len(llm.plan_inputs) == 1 and len(llm.atom_inputs) == 1
        assert (await session.status())["usage"]["spent"] == 2
        saved = await service.get_hypothesis(pid)
        assert saved.revision == 2
        assert saved.description == "Access is guarded sometimes"
        task = await session.deps.central_state.get_task(ids[0])
        assert task.hypothesis_revisions == {pid: 2}
        assert task.completion_criteria[0].hypothesis_id == pid
        assert [criterion_id(item) for item in task.completion_criteria] == [
            criterion_id(CompletionCriterion(kind="observation",
                                             description="Inspect refined",
                                             hypothesis_id=pid))]

async def test_split_loop_shared_budget_charges_both_asks_once(tmp_path):
    bad = {"tasks": [{"task_key": "t1", "question_id": "nope",
            "objective": "Probe", "title": "Probe", "role_id": "probe-worker",
            "target_node_ids": [TARGET],
            "completion_criterion_ids": [_condition_id("Inspect")]}]}
    llm = SplitDouble([_plan_two_questions()], [bad, _atom_two_tasks()])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 2
        structured = [r for r in llm.requests if r["kind"] == "complete_structured"]
        assert len(structured) == 3
        assert (await session.status())["usage"]["spent"] == 3

async def test_split_loop_atomizer_failure_commits_nothing(tmp_path):
    bad = {"tasks": [{"task_key": "t1", "question_id": "nope",
            "objective": "Probe", "title": "Probe", "role_id": "probe-worker",
            "target_node_ids": [TARGET],
            "completion_criterion_ids": [_condition_id("Inspect")]}]}
    llm = SplitDouble([_new_candidate_plan()], [bad] * 7)  # decision_max_attempts (6) + initial
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        assert failed.value.stage == "task_atomization"
        assert failed.value.kind == "budget"
        assert failed.value.diagnostic.code == "decision_repair_budget_exhausted"
        structured = [r for r in llm.requests if r["kind"] == "complete_structured"]
        expected = 2 + session.deps.settings.decision_max_attempts  # plan + atomizer + repairs
        assert failed.value.attempts == len(structured) == expected
        assert (await session.status())["usage"]["spent"] == expected
        assert await state.list_tasks() == []
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        cursor = await state.transaction(lambda tx: tx.hypothesis_planner_cursor(), write=False)
        assert cursor is None

async def test_split_loop_stop_between_asks_halts_without_commit(tmp_path):
    llm = SplitDouble([], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        async def plan_with_stop(payload):
            def cancel(tx):
                mission = tx.get_mission(tx.binding()[0])
                tx.update_mission(mission.model_copy(update={
                    "status": MissionStatus.CANCELLED,
                    "revision": mission.revision + 1}),
                    expected_revision=mission.revision)
            await state.transaction(cancel)
            return _plan_two_questions()
        llm.plans.append(plan_with_stop)
        llm.atoms.append(_atom_two_tasks())
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        assert failed.value.stage == "task_atomization"
        assert failed.value.kind == "budget"
        assert len(llm.plan_inputs) == 1
        assert len(llm.atom_inputs) == 0
        assert await state.list_tasks() == []
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        cursor = await state.transaction(lambda tx: tx.hypothesis_planner_cursor(), write=False)
        assert cursor is None
        structured = [r for r in llm.requests if r["kind"] == "complete_structured"]
        assert len(structured) == 1
        assert (await session.status())["usage"]["spent"] == 1

async def test_split_loop_related_change_between_stages_discards_all(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate as _cand
    from tests.integration.test_probe_workers import register as _register
    llm = SplitDouble([], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)
        await _register(session, "seed", updates=[_cand()], hypotheses={"local": 1})
        (seed,) = await service.list_hypotheses()
        pid = seed.hypothesis_id
        assert seed.revision == 1
        llm.plans.append({
            "updates": [], "questions": [{
                "question_id": "q1", "objective": "Probe the seeded premise",
                "target_node_ids": [TARGET],
                "completion_criteria": [{"kind": "observation",
                                        "description": "Inspect seed"}],
                "candidate_refs": [pid]}],
            "reason": "Probe the seeded premise", "termination": "continue"})
        async def atom_with_related_drift(payload):
            current = await service.get_hypothesis(pid)
            await _register(session, "changed", updates=[_cand(
                current=current.model_dump(mode="json"), status="held")],
                hypotheses={pid: 2})
            return {"tasks": [{
                "task_key": "t1", "question_id": "q1",
                "objective": "Probe the seeded premise", "title": "Seed probe",
                "role_id": "probe-worker", "target_node_ids": [TARGET],
                "completion_criterion_ids": [_condition_id("Inspect seed")],
                "probe_limits": {"max_actions": 1, "max_model_requests": 4}}]}
        llm.atoms.append(atom_with_related_drift)
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert ids == []
        structured = [r for r in llm.requests if r["kind"] == "complete_structured"]
        assert len(structured) == 2
        assert (await session.status())["usage"]["spent"] == 2
        invalidated = await state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False)
        assert bool(invalidated)
        saved = await service.get_hypothesis(pid)
        assert saved.status == "held" and saved.revision == 2

async def test_split_loop_unrelated_sibling_between_stages_allowed(tmp_path):
    from tests.integration.test_probe_workers import register as _register
    llm = SplitDouble([_plan_two_questions()], [])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        async def atom_with_sibling(payload):
            await _register(session, "sibling")
            return _atom_two_tasks()
        llm.atoms.append(atom_with_sibling)
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 2
        assert len(await session.deps.central_state.list_tasks()) == 3

async def test_split_loop_repeat_advance_consumes_cursor(tmp_path):
    llm = SplitDouble([_plan_two_questions()], [_atom_two_tasks()])
    async with probe_selection(tmp_path, llm=llm) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        first = await planner.advance(session.runtime)
        assert len(first) == 2
        assert await planner.advance(session.runtime) == []
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 1
        assert len(await session.deps.central_state.list_tasks()) == 2

async def test_split_loop_storage_resume_registers_once(tmp_path):
    from redteam.app.central_run import open_central_run
    from redteam.core.config import AppConfig, EngagementConfig
    from tests.integration.test_central_completion import OriginalProtocolDouble
    from tests.integration.test_central_development import gateway
    from tests.integration.test_central_parallel import limits
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(specialist_profile_path=str(PROFILE))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"})))
    root = tmp_path / "run"
    llm = SplitDouble([_plan_two_questions()], [_atom_two_tasks()])
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, directory=root, settings=configured,
                                 **options) as session:
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 2
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
    async with open_central_run(config, directory=root, settings=configured,
                                 resume=True, run_id=run_id,
                                 engagement_id=engagement_id, **options) as session:
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 1
        assert len(await session.deps.central_state.list_tasks()) == 2

async def test_legacy_single_stage_storage_resume_compat(tmp_path):
    from redteam.app.central_run import open_central_run
    from redteam.core.config import AppConfig, EngagementConfig
    from tests.integration.test_central_completion import OriginalProtocolDouble
    from tests.integration.test_central_development import gateway
    from tests.integration.test_central_parallel import limits
    from tests.integration.test_hypothesis_planner import candidate as _candidate
    from tests.integration.test_hypothesis_planner import probe as _probe
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    legacy = PROFILE.with_name("native-probes-legacy.yaml")
    configured = configured.with_overrides(specialist_profile_path=str(legacy))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"})))
    root = tmp_path / "run"
    llm = FakeLLMClient(canned_structured={"updates": [_candidate()], "tasks": [_probe()],
                                          "reason": "No candidate yet"})
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, directory=root, settings=configured,
                                 **options) as session:
        assert session.deps.specialist_policy.probe_atomization is False
        first = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(first) == 1
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
    async with open_central_run(config, directory=root, settings=configured,
                                 resume=True, run_id=run_id,
                                 engagement_id=engagement_id, **options) as session:
        assert session.deps.specialist_policy.probe_atomization is False
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(await session.deps.central_state.list_tasks()) == 1

async def test_split_loop_single_request_quota_blocks_atomizer(tmp_path):
    from redteam.app.central_run import open_central_run
    from redteam.core.config import AppConfig, EngagementConfig
    from tests.integration.test_central_completion import OriginalProtocolDouble
    from tests.integration.test_central_development import gateway
    from tests.integration.test_central_parallel import limits
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(
        specialist_profile_path=str(PROFILE), decision_normal_limit=1)
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"})))
    llm = SplitDouble([_new_candidate_plan()], [_atom_two_tasks()])
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, directory=tmp_path / "run",
                                 settings=configured, **options) as session:
        state = session.deps.central_state
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        assert failed.value.stage == "task_atomization"
        assert failed.value.kind == "budget"
        assert failed.value.diagnostic.code == "decision_normal_budget_exhausted"
        assert failed.value.attempts == 1
        assert len(llm.plan_inputs) == 1
        assert len(llm.atom_inputs) == 0
        assert await state.list_tasks() == []
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        cursor = await state.transaction(lambda tx: tx.hypothesis_planner_cursor(), write=False)
        assert cursor is None
        assert (await session.status())["usage"]["spent"] == 1

def _profile_run_config(profile):
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(specialist_profile_path=str(profile))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"})))
    return config, configured

async def test_legacy_bound_run_accepts_same_profile_resume(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate as _candidate
    from tests.integration.test_hypothesis_planner import probe as _probe
    legacy = PROFILE.with_name("native-probes-legacy.yaml")
    config, configured = _profile_run_config(legacy)
    root = tmp_path / "run"
    llm = FakeLLMClient(canned_structured={"updates": [_candidate()], "tasks": [_probe()],
                                          "reason": "No candidate yet"})
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, directory=root, settings=configured,
                                 **options) as session:
        assert await SpecialistPlanner(session.deps).advance(session.runtime)
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
    async with open_central_run(config, directory=root, settings=configured,
                                 resume=True, run_id=run_id,
                                 engagement_id=engagement_id, **options) as session:
        assert session.deps.specialist_policy.probe_atomization is False
        await runtime_token(session.deps, session.runtime, require_task=False)
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(await session.deps.central_state.list_tasks()) == 1

async def test_legacy_bound_run_refuses_split_profile_resume(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate as _candidate
    from tests.integration.test_hypothesis_planner import probe as _probe
    legacy = PROFILE.with_name("native-probes-legacy.yaml")
    config, legacy_configured = _profile_run_config(legacy)
    _, split_configured = _profile_run_config(PROFILE)
    root = tmp_path / "run"
    llm = FakeLLMClient(canned_structured={"updates": [_candidate()], "tasks": [_probe()],
                                          "reason": "No candidate yet"})
    tools = OriginalProtocolDouble(gateway())
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, directory=root, settings=legacy_configured,
                                 **options) as session:
        assert await SpecialistPlanner(session.deps).advance(session.runtime)
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        assert len(await session.deps.central_state.list_tasks()) == 1
    with pytest.raises(StateConflict, match="run_binding_changed_new_run_required"):
        async with open_central_run(config, directory=root, settings=split_configured,
                                     resume=True, run_id=run_id,
                                     engagement_id=engagement_id, **options):
            pytest.fail("split-profile resume of a legacy-bound run must not open")

def _new_candidate_plan():
    return {"updates": [{"expected_revision": None, "hypothesis": {
        "contract_version": 2, "hypothesis_id": "local",
        "description": "Access may be unguarded", "target_node_ids": [TARGET],
        "reason": "Assess the preserved response", "status": "open"}}],
        "questions": [{"question_id": "q1", "objective": "Probe the new premise",
         "target_node_ids": [TARGET],
         "completion_criteria": [{"kind": "observation", "description": "Inspect new"}],
         "candidate_refs": ["local"]}],
        "reason": "Probe the new premise", "termination": "continue"}

def _new_candidate_atom(payload):
    issued = payload["issued_candidate_ids"]
    assert set(issued) == {"local"}
    pid = issued["local"]
    assert all(item["hypothesis_id"] != pid for item in payload["candidates"])
    assert payload["candidate_revisions"][pid] == 1
    (question,) = [item for item in payload["questions"] if item["question_id"] == "q1"]
    assert question["hypothesis_revisions"] == {pid: 1}
    assert question["objective"] == "Probe the new premise"
    assert payload["strategy"]["reason"] == "Probe the new premise"
    (entry,) = payload["strategy"]["updates"]
    assert entry["hypothesis"]["hypothesis_id"] == pid
    assert entry["hypothesis"]["description"] == "Access may be unguarded"
    assert entry["expected_revision"] is None
    assert entry["proposed_revision"] == 1
    return {"tasks": [{"task_key": "t1", "question_id": "q1",
            "objective": "Probe the new premise", "title": "New premise probe",
            "role_id": "probe-worker", "target_node_ids": [TARGET],
            "completion_criterion_ids": [_condition_id("Inspect new")],
            "probe_limits": {"max_actions": 1, "max_model_requests": 4}}]}


async def test_reference_repair_delivers_ids_and_persists_original_conditions(tmp_path):
    class ReferenceRepairDouble(SplitDouble):
        async def complete_structured(self, *, messages, response_schema, **kwargs):
            if "tasks" in response_schema.get("properties", {}):
                if self.atom_inputs:
                    feedback = messages[-1]["content"].split("Previous attempt failed", 1)[1]
                    assert "$.tasks[0].completion_criterion_ids" in feedback
                    assert "Task t1 for question q1" in feedback
                    assert _condition_id("Inspect first") in feedback
                    assert "completion_criteria" not in response_schema["$defs"][
                        "AtomizedProbeSelection"]["properties"]
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, **kwargs,
            )

    def bad_selection(payload):
        tasks = _atom_two_tasks()["tasks"]
        tasks[0]["completion_criterion_ids"] = [
            payload["questions"][1]["completion_criteria"][0]["criterion_id"]
        ]
        return {"tasks": tasks}

    def original_selection(payload):
        tasks = _atom_two_tasks()["tasks"]
        for task, question in zip(tasks, payload["questions"], strict=True):
            task["completion_criterion_ids"] = [
                item["criterion_id"] for item in question["completion_criteria"]
            ]
        return {"tasks": tasks}

    llm = ReferenceRepairDouble([_plan_two_questions()], [bad_selection, original_selection])
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(ids) == 2 and len(llm.atom_inputs) == 2
        assert (await session.status())["usage"]["spent"] == 3
        for task_id, expected in zip(ids, ("Inspect first", "Inspect second"), strict=True):
            task = await session.deps.central_state.get_task(task_id)
            assert task.completion_criteria[0].description == expected
            assert criterion_id(task.completion_criteria[0]) == _condition_id(expected)
        assert not await session.deps.central_state.list_executions()
