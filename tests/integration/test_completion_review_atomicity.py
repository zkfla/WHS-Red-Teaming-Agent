"""Actual completion reviews may not commit after their inputs have changed."""

from __future__ import annotations

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import TaskId
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    OperatorIntentService,
)
from redteam.orchestration.paths import PathPlan
from tests.integration import _engine_llm_harness as h


class Reviewer:
    def __init__(self):
        self.hook = None
        self.criteria = []
        self.calls = 0

    async def complete_structured(self, **kwargs):
        self.calls += 1
        if self.hook:
            await self.hook()
        props = kwargs["response_schema"].get("properties", {})
        if "criteria" in props:
            return h.goal_accept(self.criteria)
        if "claims" in props:
            import json as _json
            try:
                _payload = _json.loads(kwargs["messages"][0]["content"])
                _excerpts = _payload.get("evidence_excerpts", [])
                _ids = [
                    str(e.get("evidence_id"))
                    for e in _excerpts
                    if isinstance(e, dict)
                    and str(e.get("evidence_id", "")).strip()
                ]
            except Exception:
                _ids = []
            if not _ids:
                _ids = ["proof-race"]
            return h.task_accept(_ids)
        return h.completion_accept()


async def setup(mode, reviewer=None):
    reviewer = reviewer if reviewer is not None else Reviewer()
    seed = await h.seed_host("run-review-race")
    world = h.make_world(reviewer, graph_store=seed.store)
    mission, task = h.make_mission_task(
        mission_id="mission-race", engagement_id="eng-t1", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    # Goal settlement needs completed tasks to reach the review; step/task keep running.
    task.status = TaskStatus.COMPLETED if mode == "goal" else TaskStatus.RUNNING
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-review-race", "eng-t1", mission_id="mission-race", task_id=str(task.task_id)
    )
    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    # Storage fixture: hydrate code-owned runtime fields onto minimal draft.
    raw.update(
        path_id="path-race",
        run_id=str(runtime.run_id),
        version=1,
        status="active",
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
        goal=task.objective,
    )
    if mode != "step":
        raw["steps"][0].update(status="completed", outcome="success", evidence_ids=["proof-race"])
    path = await world.paths.register(PathPlan.model_validate(raw))
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-race",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-race",
            invocation_id="inv-race",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
        )
    )
    reviewer.criteria = [
        {
            "criterion": "actual response",
            "met": True,
            "evidence_ids": ["proof-race"],
            "claims": [
                {
                    "description": "actual response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-race"],
                }
            ],
        }
    ]
    return world, runtime, path, reviewer


@pytest.mark.parametrize("mode", ["step", "task", "goal"])
@pytest.mark.parametrize("change", ["task", "progress", "intent"])
async def test_review_rejects_changed_tasks_progress_or_pending_instructions(mode, change):
    world, runtime, path, reviewer = await setup(mode)

    async def mutate():
        if change == "task":
            await world.tasks.update_priority(runtime.active_task_id, 321, now=h.utcnow())
        elif change == "progress":
            await world.paths.update_step(
                str(runtime.run_id),
                path.path_id,
                path.version,
                "step-1",
                "blocked",
                outcome="blocked",
            )
        else:
            service = OperatorIntentService(
                store=InMemoryOperatorIntentStore(),
                task_graph_store=world.tasks,
                path_store=world.paths,
            )
            received = await service.receive(
                "exclude mobile", mission_id=runtime.mission_id, run_id=runtime.run_id
            )
            assert received.intent_id is not None

    reviewer.hook = mutate
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    with pytest.raises(StateConflict):
        if mode == "step":
            await engine.complete_step(
                path,
                TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
                runtime,
                context,
            )
        elif mode == "task":
            await engine.complete_task(
                path,
                TurnDecision(mode="complete_task", reason="evaluate actual evidence"),
                runtime,
                context,
            )
        else:
            await engine.complete_goal(
                path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, context
            )
    assert reviewer.calls == 1
    # Goal setup uses a completed task so the review is reachable; step/task stay running.
    expected_task = TaskStatus.COMPLETED if mode == "goal" else TaskStatus.RUNNING
    assert (await world.tasks.get_task(runtime.active_task_id)).status is expected_task
    assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.ACTIVE


class _ScopeCapture:
    """Record review requests; answer the step review from a script."""

    def __init__(self, *, accepted: bool, reason: str):
        self.calls = 0
        self.systems: list[str] = []
        self.contents: list[str] = []
        self.payloads: list[dict] = []
        self._answer = {"accepted": accepted, "reason": reason}

    async def complete_structured(self, **kwargs):
        self.calls += 1
        self.systems.append(kwargs.get("system") or "")
        self.contents.append(str(kwargs.get("messages") or ""))
        for message in kwargs.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                try:
                    import json as _json

                    parsed = _json.loads(content)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    self.payloads.append(parsed)
        return dict(self._answer)

    async def complete(self, **kwargs):
        return ""


@pytest.mark.parametrize("mode", ["step", "task", "goal"])
async def test_review_sees_current_registered_facts_and_partial_recording(mode):
    from redteam.orchestration.graph_recording import (
        GRAPH_RECORD_TOOL_ID,
        GraphRecordingHandler,
        graph_recording_descriptor,
    )

    class Capture(Reviewer):
        async def complete_structured(self, **kwargs):
            self.payload = json.loads(kwargs["messages"][0]["content"])
            return await super().complete_structured(**kwargs)

    import json

    reviewer = Capture()
    world, runtime, path, _ = await setup(mode, reviewer=reviewer)
    world.deps.local_tools.register(graph_recording_descriptor(), GraphRecordingHandler())
    engine = InvestigationEngine(world.deps)
    stale_context = await engine.context(runtime, GraphObservation())
    assert stale_context["local_tool_results"] == []
    assert not any(
        "inventory_observation" in item.get("observed_facts", {})
        for item in stale_context["knowledge"]["facts"]
    )
    node_id = path.steps[0].target_node_ids[0]
    await engine.execute_local_calls(
        [{"tool_id": GRAPH_RECORD_TOOL_ID, "arguments": {"facts": [
            {"evidence_id": "proof-race", "raw_path": ["inventory"], "value": "ok",
             "fact_name": "inventory_observation", "target_node_id": node_id},
            {"evidence_id": "proof-race", "raw_path": ["inventory"], "value": "wrong",
             "fact_name": "wrong_inventory", "target_node_id": node_id},
        ]}}],
        runtime=runtime, path=path, budget=engine._decision_budget(),
    )
    if mode == "step":
        _, errors = await engine.complete_step(
            path, TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
            runtime, stale_context,
        )
    elif mode == "task":
        errors = await engine.complete_task(
            path, TurnDecision(mode="complete_task", reason="registered observation"),
            runtime, stale_context,
        )
    else:
        errors = await engine.complete_goal(
            path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, stale_context,
        )
    assert errors == []
    assert reviewer.calls == 1
    facts = reviewer.payload["knowledge"]["facts"]
    assert any(item["observed_facts"].get("inventory_observation") == "ok" for item in facts)
    assert all("wrong_inventory" not in item["observed_facts"] for item in facts)
    result = reviewer.payload["local_tool_results"][0]["result"]
    assert result["ok"] is False
    assert len(result["payload"]["accepted_facts"]) == 1
    assert len(result["payload"]["refused_facts"]) == 1
    assert stale_context["local_tool_results"] == []


async def test_step_review_request_carries_step_scope_only():
    reviewer = _ScopeCapture(accepted=True, reason="step objective met on stored evidence")
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    finished, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
        runtime,
        context,
    )

    assert errors == []
    assert reviewer.calls == 1
    system = reviewer.systems[0]
    assert "supplied path step only" in system
    assert "running mission task" in system
    content = reviewer.contents[0]
    assert "confirm the host answers inventory" in content
    assert "harness completion condition" in content
    # Stage-only progress: the step completes while its task still runs.
    step = next(s for s in finished.steps if s.step_id == "step-1")
    assert str(step.status) == "completed"
    assert (await world.tasks.get_task(runtime.active_task_id)).status is TaskStatus.RUNNING
    assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.ACTIVE


async def test_step_review_rejection_returns_the_actual_reason():
    marker = "inventory shows the port closed, no banner captured"
    reviewer = _ScopeCapture(accepted=False, reason=marker)
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    _, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
        runtime,
        context,
    )

    assert reviewer.calls == 1
    assert len(errors) == 1
    assert marker in errors[0]
    assert "semantic completion review rejected" in errors[0]
    # The next decision consumes the actual reason through safe feedback.
    from redteam.orchestration.cognition import DecisionBudget

    assert marker in " ".join(engine._safe_errors(errors, DecisionBudget(3)))


async def test_step_review_without_evidence_keeps_the_guard():
    reviewer = _ScopeCapture(accepted=True, reason="must not be reached")
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    _, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step([])),
        runtime,
        context,
    )

    assert errors != []
    assert reviewer.calls == 0


async def test_each_goal_criterion_records_only_its_actual_evidence_owner():
    world, runtime, path, reviewer = await setup("goal")
    mission = await world.tasks.get_mission(runtime.mission_id)
    await world.tasks.update_mission(
        mission.model_copy(
            update={
                "success_criteria": ["first response", "second response"],
                "revision": mission.revision + 1,
            }
        )
    )
    task = await world.tasks.get_task(runtime.active_task_id)
    first = task.model_copy(
        update={"task_id": TaskId("task-first"), "status": TaskStatus.COMPLETED}
    )
    await world.tasks.create_task(first)
    # A historical accepted path retains the first task's own measured receipt.
    first_path = path.model_copy(
        deep=True,
        update={
            "path_id": "path-first",
            "status": "candidate",
            "mission_task_id": str(first.task_id),
        },
    )
    first_path.steps[0].evidence_ids = ["proof-first"]
    # Register before restoring the existing active path, so the stored snapshot
    # is accepted while its evidence ownership remains immutable.
    first_path.status = "active"
    await world.paths.register(first_path, expected_version=1, expected_path_id=path.path_id)
    current = path.model_copy(deep=True, update={"version": 2, "parent_version": 1})
    current = await world.paths.register(
        current, expected_version=1, expected_path_id=first_path.path_id
    )
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-first",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-first",
            invocation_id="inv-first",
            path_id=first_path.path_id,
            path_version=1,
            step_id="step-1",
            target_node_id=path.steps[0].target_node_ids[0],
        )
    )
    reviewer.criteria = [
        {
            "criterion": "first response",
            "met": True,
            "evidence_ids": ["proof-first"],
            "claims": [
                {
                    "description": "actual first response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-first"],
                }
            ],
        },
        {
            "criterion": "second response",
            "met": True,
            "evidence_ids": ["proof-race"],
            "claims": [
                {
                    "description": "actual second response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-race"],
                }
            ],
        },
    ]
    engine = InvestigationEngine(world.deps)
    errors = await engine.complete_goal(
        current,
        TurnDecision.model_validate(h.turn_complete_goal()),
        runtime,
        await engine.context(runtime, GraphObservation()),
    )
    assert errors == []
    final = await world.tasks.get_mission(runtime.mission_id)
    assert final.status is MissionStatus.COMPLETED
    results = {item.criterion: item for item in final.criterion_results}
    assert results["first response"].task_ids == [first.task_id]
    assert results["second response"].task_ids == [task.task_id]


@pytest.mark.parametrize("accepted", [True, False])
async def test_narrowed_step_payload_keeps_constraints_and_sources(accepted):
    reason = (
        "step objective met on stored evidence"
        if accepted
        else "narrowed review still rejects"
    )
    reviewer = _ScopeCapture(accepted=accepted, reason=reason)
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    proposal = h.turn_complete_step(["proof-race"])["completion"]
    old_sources = [
        await EvidenceReader(world.evidence).read(
            evidence_id="proof-race", run_id=str(runtime.run_id), limit=4000
        )
    ]
    old_payload = {
        **context,
        "step": path.steps[0].model_dump(mode="json"),
        "completion_proposal": proposal,
        "source_results": old_sources,
    }
    finished, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
        runtime,
        context,
    )
    assert reviewer.calls == 1
    assert len(reviewer.payloads) == 1
    new_payload = reviewer.payloads[0]
    # Permission constraints survive the narrowing on both verdicts.
    assert "evidence_read_contract" in new_payload
    assert "requested_evidence" in new_payload
    assert "engagement_constraints" in new_payload or "operator_constraints" in new_payload
    assert "operator_instructions" in new_payload
    # Snapshot reference stays under the event-compatible key.
    assert new_payload["active_path"]["path_id"] == path.path_id
    assert new_payload["active_path"]["version"] == path.version
    # Step scope, proposal, and proven source ids stay present.
    assert new_payload["step"]["step_id"] == "step-1"
    assert new_payload["completion_proposal"]["evidence_ids"] == ["proof-race"]
    assert new_payload["source_evidence_ids"] == ["proof-race"]
    assert any(
        result.get("evidence_id") == "proof-race" for result in new_payload["source_results"]
    )
    # Planning-only context leaves the review payload.
    for dropped in ("tools", "tools_by_role", "execution_history", "success_criteria"):
        assert dropped not in new_payload
    import json as _json

    old_size = len(_json.dumps(old_payload, sort_keys=True))
    new_size = len(_json.dumps(new_payload, sort_keys=True))
    assert new_size < old_size
    if accepted:
        assert errors == []
        assert finished.steps[0].status == "completed"
    else:
        assert len(errors) == 1


async def test_same_decide_read_then_complete_uses_fresh_window():
    import json as _json

    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-read-complete")
    world = h.make_world(llm, graph_store=seed.store)
    mission, task = h.make_mission_task(
        mission_id="mission-read-complete", engagement_id="eng-t1", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-read-complete", "eng-t1",
        mission_id="mission-read-complete", task_id=str(task.task_id),
    )
    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    raw.update(
        path_id="path-read-complete",
        run_id=str(runtime.run_id),
        version=1,
        status="active",
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
        goal=task.objective,
    )
    path = await world.paths.register(PathPlan.model_validate(raw))
    marker = "TAIL-WINDOW-MARKER-7f3a"
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-long",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-long",
            invocation_id="inv-long",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
            output={"blob": "A" * 6000 + marker + "B" * 500},
        )
    )
    reader = EvidenceReader(world.evidence)
    full = await reader.read(evidence_id="proof-long", run_id=str(runtime.run_id), limit=20000)
    assert full["status"] == "ok"
    marker_at = full["text"].find(marker)
    assert marker_at > 4000
    llm.add(
        "next_action",
        {
            "mode": "read_evidence",
            "reason": "read the later window",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {
                "evidence_id": "proof-long",
                "offset": marker_at - 10,
                "limit": 4000,
            },
            "hypothesis_update": {},
        },
    )
    llm.add("next_action", h.turn_complete_step(["proof-long"]))
    llm.add(
        "next_action",
        {**h.turn_complete_step(["proof-long"]), "mode": "hold", "reason": "done"},
    )
    llm.add("completion_review", h.completion_accept())
    engine = InvestigationEngine(world.deps)
    decided, compiled, _ = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    step = next(s for s in decided.steps if s.step_id == "step-1")
    assert str(step.status) == "completed"
    review_calls = [call for call in llm.calls if call["route"] == "completion_review"]
    assert len(review_calls) == 1
    payload = _json.loads(review_calls[0]["messages"][0]["content"])
    windows = payload.get("requested_evidence", [])
    assert windows != []
    assert any(marker in str(window.get("text", "")) for window in windows)
    # The narrowed review reads sources from offset 0 (front excerpt),
    # so the late marker must survive only via the refreshed read window.
    front_sources = payload.get("source_results", [])
    assert front_sources != []
    assert all(marker not in str(item.get("text", "")) for item in front_sources)


async def test_delegated_finish_keeps_both_windows_for_same_proof():
    """Actual specialist finish keeps root front + scoped back windows."""
    import json as _json

    from redteam.orchestration.graph_recording import (
        GRAPH_RECORD_TOOL_ID,
        GraphRecordingHandler,
        graph_recording_descriptor,
    )
    from tests.unit.test_autonomous_delegation import _delegated_setup

    ctx = await _delegated_setup("run-del-scoped-review")
    ctx.engine.deps.local_tools.register(graph_recording_descriptor(), GraphRecordingHandler())
    marker = "SCOPED-TAIL-MARKER-9c1e"
    proof_id = "proof-scoped"
    await ctx.world.evidence.append(
        h.make_tool_evidence(
            evidence_id=proof_id,
            run_id=str(ctx.runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-scoped",
            invocation_id="inv-scoped",
            path_id=ctx.path.path_id,
            path_version=ctx.path.version,
            step_id="step-1",
            target_node_id=ctx.seed.node_id,
            output={"blob": "A" * 6000 + marker + "B" * 500, "inventory": "ok"},
        )
    )
    reader = EvidenceReader(ctx.world.evidence)
    full = await reader.read(
        evidence_id=proof_id, run_id=str(ctx.runtime.run_id), limit=20000
    )
    assert full["status"] == "ok"
    marker_at = full["text"].find(marker)
    assert marker_at > 4000
    # Root already read the front; the specialist scoped-read the back.
    ctx.runtime.evidence_reads = [
        {
            "request": {
                "evidence_id": proof_id,
                "offset": 0,
                "limit": 4000,
                "query": None,
            },
            "status": "ok",
            "delegation_id": None,
        },
        {
            "request": {
                "evidence_id": proof_id,
                "offset": marker_at - 10,
                "limit": 4000,
                "query": None,
            },
            "status": "ok",
            "delegation_id": ctx.delegation_id,
        },
    ]
    ctx.llm.add("worker_next_action", {
        "decision": "tool_calls",
        "tool_calls": [{"tool_id": GRAPH_RECORD_TOOL_ID, "arguments": {"facts": [
            {"evidence_id": proof_id, "raw_path": ["inventory"], "value": "ok",
             "fact_name": "worker_inventory", "target_node_id": ctx.seed.node_id},
        ]}}],
    })
    await ctx.engine.decide(ctx.runtime, GraphObservation())
    ctx.runtime = ctx.engine.updated_runtime
    ctx.llm.add(
        "worker_next_action",
        {
            "decision": "finish",
            "summary": "scoped sweep done",
            "finish_reason": "objective_done",
            "evidence_ids": [proof_id],
            "completion": {
                "outcome": "success",
                "reason": "scoped sweep proves inventory",
                "evidence_ids": [proof_id],
            },
            "unresolved": [],
        },
    )
    ctx.llm.add(
        "completion_review", h.completion_accept("scoped review keeps both")
    )
    _, candidates, note = await ctx.engine.decide(
        ctx.runtime, GraphObservation()
    )
    assert candidates == []
    assert note == "continue_planning"
    review_calls = [
        call for call in ctx.llm.calls if call["stage"] == "completion_review"
    ]
    assert len(review_calls) == 1
    payload = _json.loads(review_calls[0]["messages"][0]["content"])
    windows = payload.get("requested_evidence", [])
    same_proof = [
        window
        for window in windows
        if isinstance(window, dict)
        and str(window.get("evidence_id", "")) == proof_id
    ]
    assert len(same_proof) == 2
    assert any(marker not in str(window.get("text", "")) for window in same_proof)
    assert any(marker in str(window.get("text", "")) for window in same_proof)
    front_sources = payload.get("source_results", [])
    assert front_sources != []
    assert all(marker not in str(item.get("text", "")) for item in front_sources)
    active = await ctx.world.paths.get_active(str(ctx.runtime.run_id))
    step = next(item for item in active.steps if item.step_id == "step-1")
    assert str(step.status) == "completed"
    assert any(
        item["observed_facts"].get("worker_inventory") == "ok"
        for item in payload["knowledge"]["facts"]
    )
    assert payload["local_tool_results"][0]["delegation_id"] == ctx.delegation_id
    assert len(payload["local_tool_results"][0]["result"]["payload"]["accepted_facts"]) == 1
