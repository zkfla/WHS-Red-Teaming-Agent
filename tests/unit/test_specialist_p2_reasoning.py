"""P2 model-only isolation and durable request-pool regressions."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.core.ports.llm import StructuredCompletion
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.delegated_runtime import decide_delegations
from redteam.orchestration.delegation import DelegationRequest
from redteam.orchestration.delegation_decision import open_delegation
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.parallel_reasoning import reserve, settle, subject_stamp
from redteam.orchestration.paths import PathPlan, SQLitePathStore
from redteam.orchestration.runtime_observation import RuntimeObservation
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph import SQLiteTaskGraphStore
from tests.integration import _engine_llm_harness as h
from tests.unit.test_coordination_sqlite import ENG, RUN, records, seed, stores
from tests.unit.test_specialist_profile import WEB_ROLE, _application, _atomized

P2_PROFILE = "config/agents/specialists-p2.yaml"


class CrossingLLM:
    """Barrier-backed fixture: completion order differs from application order."""

    def __init__(self, *, mutate=None, invalid=False):
        self.entered = asyncio.Event()
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.mutate = mutate
        self.invalid = invalid

    async def complete_structured_with_usage(
        self, *, messages, response_schema, system=None, **kwargs,
    ):
        payload, _ = json.JSONDecoder().raw_decode(messages[-1]["content"])
        identity = payload["path_step_id"]
        self.calls.append(identity)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if len(self.calls) >= 2:
            self.entered.set()
        try:
            await asyncio.wait_for(self.entered.wait(), timeout=2)
            if identity == "s1":
                await asyncio.sleep(0.01)
            if self.mutate is not None and len(self.calls) == 2:
                mutate, self.mutate = self.mutate, None
                await mutate()
            if self.invalid:
                return StructuredCompletion({"off_schema": True}, total_tokens=7)
            return StructuredCompletion({
                "decision": "hold", "reason": "Bounded fixture observation",
                "summary": "held-" + identity, "unresolved": ["need-" + identity],
            }, total_tokens=11 if identity == "s1" else 23)
        finally:
            self.active -= 1

    async def complete(self, **kwargs):
        return ""


async def world(tmp_path, llm, *, pool=32):
    seeded, eng, deps, gateway = await _application(tmp_path, llm)
    loaded = load_specialist_profile(P2_PROFILE)
    # Assemble through the actual App profile owner, not a role-name branch.
    from redteam.app.runtime_components import RuntimeComponents

    components = loaded.assemble(RuntimeComponents(
        agents=deps.agents, candidate_providers=deps.candidate_providers,
        verifier=deps.verifier, result_mappers=deps.result_mappers,
    ))
    deps = replace(
        deps, agents=components.agents, candidate_providers=components.candidate_providers,
        planning_provider=components.planning_provider,
        settings=loaded.configure_execution(OrchestrationSettings()).model_copy(update={
            "specialist_decision_pool_limit": pool,
        }),
    )
    runtime = h.make_runtime("run-specialist", str(eng.engagement_id))
    value = _atomized(seeded.node_id)["path"]
    second = deepcopy(value["steps"][0])
    second.update(step_id="s2", objective="An independent reasoning purpose")
    value["steps"].append(second)
    path = PathPlan.model_validate({
        **value, "run_id": str(runtime.run_id), "path_id": "path", "version": 1,
        "status": "active", "goal": "Observe synthetic subjects",
    })
    await deps.path_store.register(path)
    for step in path.steps:
        delegation = open_delegation(
            DelegationRequest(path_step_id=step.step_id, agent_id=WEB_ROLE,
                              objective=step.objective),
            path_id=path.path_id, path_version=1, mission_task_id=None, decision_limit=8,
        )
        runtime.delegations[delegation.delegation_id] = delegation
    engine = InvestigationEngine(deps)
    engine._event_runtime = engine.updated_runtime = runtime
    context = await engine.context(runtime, await seeded.service.observe(step=0))
    return seeded, deps, gateway, engine, path, runtime, context


def test_p2_requires_explicit_finite_pool_and_preserves_p1_default():
    with pytest.raises(ValueError, match="finite_pool"):
        OrchestrationSettings(specialist_decision_concurrency=2)
    p1 = load_specialist_profile("config/agents/specialists-p1.yaml")
    assert p1.configure_execution(OrchestrationSettings()).specialist_decision_concurrency == 1
    p2 = load_specialist_profile(P2_PROFILE)
    settings = p2.configure_execution(OrchestrationSettings())
    assert settings.specialist_decision_concurrency == 2
    assert settings.specialist_decision_pool_limit == 64


async def test_crossed_replies_keep_purpose_usage_and_events_isolated(tmp_path):
    llm = CrossingLLM()
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)
    actions, advanced = await decide_delegations(engine, path, runtime, context)
    assert actions == [] and advanced
    assert llm.maximum == 2 and set(llm.calls) == {"s1", "s2"}
    current = engine.updated_runtime
    assert current.model_requests_used == 2 and current.model_tokens_used == 34
    by_step = {d.path_step_id: d for d in current.delegations.values()}
    assert by_step["s1"].tokens_used == 11 and by_step["s2"].tokens_used == 23
    assert by_step["s1"].summary == "held-s1" and by_step["s2"].summary == "held-s2"
    assert all(d.decision_attempts_used == 1 for d in by_step.values())
    assert gateway.invocations == []
    assert engine.active_delegation is None and engine.decision_token_limit is None
    events = deps.event_bus.history(event_type="AgentTurnDecided")
    by_id = {d.delegation_id: d for d in current.delegations.values()}
    assert len(events) == 2
    assert all(e.payload["path_step_id"] == by_id[e.payload["delegation_id"]].path_step_id
               and e.payload["agent_id"] == WEB_ROLE for e in events)
    saved = await records(coordinator_for(deps.task_graph_store, deps.path_store))
    pool = saved[("specialist_reasoning_pool", str(runtime.run_id))]
    assert pool["spent"] == 2 and pool["reservations"] == {}


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_worker_receives_only_its_cited_action_control_history(tmp_path, concurrency):
    class CaptureModel:
        def __init__(self):
            self.payloads = []

        async def complete_structured_with_usage(self, *, messages, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(messages[-1]["content"])
            self.payloads.append(payload)
            return StructuredCompletion({"decision": "hold", "reason": "Await new evidence",
                                         "unresolved": ["No new target call needed"]})

    llm = CaptureModel()
    seeded, deps, gateway, engine, path, runtime, _ = await world(tmp_path, llm)
    deps = replace(deps, settings=deps.settings.model_copy(update={
        "specialist_decision_concurrency": concurrency,
    }))
    engine = InvestigationEngine(deps)
    for state in runtime.delegations.values():
        identity = state.path_step_id
        source = h.make_tool_evidence(
            evidence_id="source-" + identity, run_id=str(runtime.run_id),
            engagement_id=str(runtime.engagement_id), action_id="action-" + identity,
            invocation_id="invoke-" + identity, path_id=path.path_id,
            path_version=path.version, step_id=identity, target_node_id=seeded.node_id,
        )
        await deps.evidence_store.append(source)
        state.evidence_ids = [str(source.evidence_id)]
        runtime.recent_observations.append(RuntimeObservation(
            kind="control", summary="Existing admission", outcome="allow", step=1,
            action_id="action-" + identity,
        ))
    runtime.recent_observations.append(RuntimeObservation(
        kind="control", summary="Unrelated action", outcome="deny", step=1,
        action_id="action-other",
    ))
    engine._event_runtime = engine.updated_runtime = runtime
    context = await engine.context(runtime, await seeded.service.observe(step=1))
    await decide_delegations(engine, path, runtime, context)
    assert len(llm.payloads) == 2
    for payload in llm.payloads:
        assert [row["action_id"] for row in payload["runtime_observations"]] == [
            "action-" + payload["path_step_id"],
        ]
        assert payload["evidence_validity"][0]["status"] == "unbound"
        assert "meaning" in payload["evidence_validity"][0]
    assert gateway.invocations == []


async def test_related_change_rejects_prepared_reply_without_target_execution(tmp_path):
    llm = CrossingLLM()
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)

    async def change():
        await deps.path_store.update_step(str(runtime.run_id), path.path_id, 1, "s1", "blocked")

    llm.mutate = change
    actions, _ = await decide_delegations(engine, path, runtime, context)
    assert not actions and not gateway.invocations
    by_step = {d.path_step_id: d for d in engine.updated_runtime.delegations.values()}
    assert "specialist_decision_context_changed" in " ".join(by_step["s1"].unresolved)
    assert by_step["s2"].summary == "held-s2"
    assert engine.updated_runtime.model_requests_used == 2


async def test_unrelated_step_progress_does_not_stale_other_subject(tmp_path):
    _, deps, _, engine, path, runtime, _ = await world(tmp_path, CrossingLLM())
    state = next(d for d in runtime.delegations.values() if d.path_step_id == "s2")
    before = await subject_stamp(engine, runtime, state)
    await deps.path_store.update_step(str(runtime.run_id), path.path_id, 1, "s1", "blocked")
    assert await subject_stamp(engine, runtime, state) == before


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_parallel_finishes_apply_latest_progress_without_replaying_sources(
    tmp_path, reverse, backend,
):
    class FinishingModel(CrossingLLM):
        async def complete_structured_with_usage(self, *, messages, response_schema, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(messages[-1]["content"])
            if "decision" not in response_schema.get("properties", {}):
                return StructuredCompletion(h.completion_accept(), total_tokens=5)
            await super().complete_structured_with_usage(
                messages=messages, response_schema=response_schema, **kwargs,
            )
            identity = payload["path_step_id"]
            return StructuredCompletion({
                "decision": "finish", "reason": "Original observation already stored",
                "summary": "Observed the assigned response", "evidence_ids": ["source-" + identity],
                "completion": {"outcome": "success", "reason": "Original response stored",
                               "evidence_ids": ["source-" + identity]},
            }, total_tokens=11)

    llm = FinishingModel()
    seeded, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)
    if backend == "sqlite":
        database = tmp_path / "coordination.db"
        deps = replace(deps, path_store=SQLitePathStore(database),
                       task_graph_store=SQLiteTaskGraphStore(database), coordinator=None)
        await deps.path_store.register(path)
        engine = InvestigationEngine(deps)
        engine._event_runtime = engine.updated_runtime = runtime
    originals = []
    for state in runtime.delegations.values():
        identity = state.path_step_id
        source = h.make_tool_evidence(
            evidence_id="source-" + identity, run_id=str(runtime.run_id),
            engagement_id=str(runtime.engagement_id), action_id="action-" + identity,
            invocation_id="invoke-" + identity, path_id=path.path_id,
            path_version=path.version, step_id=identity, target_node_id=seeded.node_id,
        )
        await deps.evidence_store.append(source)
        originals.append(source.model_dump(mode="json"))
        state.evidence_ids = [str(source.evidence_id)]
    if reverse:
        runtime.delegations = dict(reversed(list(runtime.delegations.items())))
    actions, advanced = await decide_delegations(engine, path, runtime, context)
    assert not actions and advanced and gateway.invocations == []
    current = await deps.path_store.get_active(str(runtime.run_id))
    assert all(s.status == "completed" and s.outcome == "success" for s in current.steps)
    assert all(d.status == "finished" for d in engine.updated_runtime.delegations.values())
    assert engine.updated_runtime.model_requests_used == 4  # two proposals, two reviews
    assert llm.maximum == 2
    for original in originals:
        saved = await deps.evidence_store.get(original["evidence_id"])
        assert saved.model_dump(mode="json") == original
    pool = (await records(coordinator_for(deps.task_graph_store, deps.path_store)))[
        ("specialist_reasoning_pool", str(runtime.run_id))
    ]
    assert pool["spent"] == 4 and pool["reservations"] == {}
    if backend == "sqlite":
        reopened = await SQLitePathStore(database).get_active(str(runtime.run_id))
        assert reopened.model_dump(mode="json") == current.model_dump(mode="json")


@pytest.mark.parametrize("change", ["version", "path", "task"])
async def test_prepared_reply_cannot_adopt_a_different_path_binding(tmp_path, change):
    llm = CrossingLLM()
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)

    async def mutate():
        update = {"version": 2, "parent_version": 1}
        if change == "path":
            update = {"path_id": "replacement-path"}
        elif change == "task":
            update["mission_task_id"] = "replacement-task"
        await deps.path_store.register(path.model_copy(update=update), expected_version=1)

    llm.mutate = mutate
    actions, _ = await decide_delegations(engine, path, runtime, context)
    assert not actions and not gateway.invocations
    assert all(d.summary not in {"held-s1", "held-s2"}
               for d in engine.updated_runtime.delegations.values())
    current = await deps.path_store.get_active(str(runtime.run_id))
    assert all(s.status == "pending" for s in current.steps)
    assert engine.updated_runtime.model_requests_used == 2


async def test_invalid_parallel_replies_spend_one_pool_without_reset(tmp_path):
    llm = CrossingLLM(invalid=True)
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)
    await decide_delegations(engine, path, runtime, context)
    assert len(llm.calls) == 8  # first request plus bounded three repairs per purpose
    assert engine.updated_runtime.model_requests_used == 8
    assert engine.updated_runtime.model_tokens_used == 56
    assert all(d.decision_attempts_used == 4 for d in engine.updated_runtime.delegations.values())
    saved = await records(coordinator_for(deps.task_graph_store, deps.path_store))
    assert saved[("specialist_reasoning_pool", str(runtime.run_id))]["spent"] == 8
    assert gateway.invocations == []


async def test_exhausted_pool_closes_purpose_without_zero_cost_retry_loop(tmp_path):
    llm = CrossingLLM()
    llm.entered.set()  # Only one request fits; a second model must never start.
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm, pool=1)
    await decide_delegations(engine, path, runtime, context)
    states = {d.path_step_id: d for d in engine.updated_runtime.delegations.values()}
    assert llm.calls == ["s1"]
    assert states["s2"].status == "finished"
    assert states["s2"].termination_reason == "specialist_decision_pool_exhausted"
    before = await deps.evidence_store.list_for_run(runtime.run_id)
    await decide_delegations(engine, path, engine.updated_runtime, context)
    assert llm.calls == ["s1"] and gateway.invocations == []
    assert len(await deps.evidence_store.list_for_run(runtime.run_id)) == len(before)
    saved = await records(coordinator_for(deps.task_graph_store, deps.path_store))
    assert saved[("specialist_reasoning_pool", str(runtime.run_id))]["spent"] == 1


async def test_role_concurrency_limit_still_serializes_model_calls(tmp_path):
    llm = CrossingLLM()
    llm.entered.set()
    _, deps, _, engine, path, runtime, context = await world(tmp_path, llm)
    definition = deps.agents.definition(WEB_ROLE)
    assert definition is not None
    definition.max_concurrency = 1
    await decide_delegations(engine, path, runtime, context)
    assert llm.maximum == 1 and len(llm.calls) == 2
    assert engine.updated_runtime.model_tokens_used == 34


async def test_cancellation_drains_models_and_keeps_uncertain_reservations_charged(tmp_path):
    llm = CrossingLLM()
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)
    release = asyncio.Event()
    original = llm.complete_structured_with_usage

    async def blocked(**kwargs):
        await original(**kwargs)
        await release.wait()

    llm.complete_structured_with_usage = blocked
    task = asyncio.create_task(decide_delegations(engine, path, runtime, context))
    await asyncio.wait_for(llm.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.active == 0 and gateway.invocations == []
    saved = await records(coordinator_for(deps.task_graph_store, deps.path_store))
    pool = saved[("specialist_reasoning_pool", str(runtime.run_id))]
    assert len(pool["reservations"]) == 2
    assert sum(pool["reservations"].values()) <= pool["limit"]


async def test_pool_reservations_survive_sqlite_reopen_and_cannot_reset(tmp_path):
    db, audit = tmp_path / "state.db", tmp_path / "audit.db"
    tasks, paths, _, svc, _ = await seed(db, audit)
    deps = SimpleNamespace(settings=OrchestrationSettings(
        specialist_decision_concurrency=2, specialist_decision_pool_limit=5,
    ), coordinator=svc._coordinator, task_graph_store=tasks, path_store=paths)
    engine = SimpleNamespace(deps=deps)
    runtime = h.make_runtime(str(RUN), str(ENG))
    runtime.mission_id = "atomic-mission"
    first, amount = await reserve(engine, runtime, 4)
    assert amount == 4
    tasks2, paths2, _, svc2, _ = stores(db, audit)
    reopened = SimpleNamespace(deps=SimpleNamespace(
        **{**vars(deps), "coordinator": svc2._coordinator,
           "task_graph_store": tasks2, "path_store": paths2},
    ))
    second, amount = await reserve(reopened, runtime, 4)
    assert amount == 1
    assert (await reserve(reopened, runtime, 1))[1] == 0
    await settle(reopened, runtime, first, 2)
    await settle(reopened, runtime, first, 2)
    assert (await reserve(reopened, runtime, 4))[1] == 2
    with pytest.raises(ValueError, match="overdrawn"):
        await settle(reopened, runtime, second, 2)
