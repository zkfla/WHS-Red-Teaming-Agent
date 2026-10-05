"""Prompt truncation is independent of original execution validation."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import Confidence, Provenance, build_properties
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType, PolicyVerdict
from redteam.orchestration.engine import (
    ActionIntent,
    InvestigationEngine,
    _collect_check_assessment,
    validate_current_action,
)
from redteam.orchestration.knowledge import collect_assessment, project_knowledge
from redteam.orchestration.nodes import execute_node, observe_node, policy_node
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.progress import knowledge_fingerprint
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.scheduler import WorkScheduler
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import initial_graph_state
from redteam.orchestration.work.models import WorkItem, WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h

RUN, ENG = "run-original-facts", "eng-t1"


async def add_measured(
    world,
    host,
    *,
    run=RUN,
    engagement=ENG,
    confidence=Confidence.MEASURED,
    status_code=200,
    evidence_suffix="",
):
    service = AttackGraphService(world.graph_store)
    identity = f"proof-{host}{evidence_suffix}"
    node = await service.upsert_node(
        GraphNodeType.HOST,
        key=host,
        facts={"status_code": status_code},
        provenance=Provenance(
            step=1,
            source="tool:inventory",
            run_id=run,
            evidence_id=identity,
            confidence=confidence,
        ),
        step=1,
    )
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id=identity,
            run_id=run,
            engagement_id=engagement,
            action_id=f"act-{host}",
            invocation_id=f"inv-{host}",
            path_id="observed",
            path_version=1,
            step_id="observe",
            target_node_id=str(node.id),
            output={"status_code": status_code},
        )
    )
    return str(node.id)


@pytest.fixture
async def world():
    llm = h.ScriptedLLM()
    w = h.make_world(llm)
    identities = [await add_measured(w, f"host-{index:03}") for index in range(110)]
    runtime = h.make_runtime(RUN, ENG)
    return SimpleNamespace(
        w=w, llm=llm, runtime=runtime, identities=identities, engine=InvestigationEngine(w.deps)
    )


def path_draft(identity):
    raw = h.draft_path(identity)
    raw["steps"][0]["required_fact_refs"] = [identity + "#fact:status_code"]
    return raw


def _plan_for_register(draft, runtime, context):
    draft.update(
        {
            "path_id": "test-path",
            "run_id": str(runtime.run_id),
            "version": 1,
            "status": "draft",
            "goal": context.get("goal", ""),
        }
    )
    return PathPlan.model_validate(draft)


def action(identity):
    return ActionIntent.model_validate(h.turn_actions(identity)["actions"][0])


async def register(world, identity, context=None):
    context = context or await world.engine.context(world.runtime, GraphObservation())
    world.llm.add("path_review", h.review_accept())
    return await world.engine.register_path(
        _plan_for_register(path_draft(identity), world.runtime, context),
        world.runtime,
        context,
        GraphObservation(),
    )


async def test_omitted_measured_target_and_fact_pass_all_execution_boundaries(world):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    summary = context["knowledge"]
    assert len(summary["known_targets"]) == len(summary["facts"]) == 100
    assert identity in summary["omitted_known_target_ids"]
    assert identity in summary["omitted_fact_node_ids"]
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert errors == []
    assert len(compiled) == 1
    assert await validate_current_action(world.w.deps, world.runtime, compiled[0].plan_step) == []
    # Review still receives the bounded projection, never the uncapped index.
    assert len(world.llm.calls) == 1
    assert identity in summary["omitted_known_target_ids"]
    assert (
        len((await world.engine.context(world.runtime, GraphObservation()))["knowledge"]["facts"])
        == 100
    )


@pytest.mark.parametrize("withdraw", [None, "before_policy", "before_execution"])
async def test_original_authority_reaches_policy_and_actual_tool_call(world, withdraw):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    assert identity in context["knowledge"]["omitted_known_target_ids"]
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert not errors
    step = compiled[0].plan_step
    gateway = DummyToolGateway()
    deps = replace(
        build_test_application(
            graph_store=world.w.graph_store,
            evidence_store=world.w.evidence,
            tool_catalog=world.w.catalog,
            tool_gateway=gateway,
        ),
        path_store=world.w.paths,
        task_graph_store=world.w.tasks,
        work_store=world.w.work,
    )
    proposal = ActionProposal(
        action_id="action-original-authority",
        run_id=world.runtime.run_id,
        engagement_id=world.runtime.engagement_id,
        agent_id=h.RECON_AGENT,
        capability=step.capability,
        operation=step.operation,
        binding_mode=step.binding_mode,
        tool_id=step.tool_id,
        target=step.resolved_target(),
        arguments=step.arguments,
        availability_impact=step.availability_impact,
        reason="execute original measured target",
        created_at=h.utcnow(),
    )
    state = initial_graph_state(world.runtime)
    state.update(plan_step=step, action_proposal=proposal)
    if withdraw == "before_policy":
        await world.w.graph_store.remove_node(identity)
    state = await policy_node(state, deps=deps)
    if withdraw == "before_policy":
        assert state["policy_decision"].verdict == PolicyVerdict.DENY
        assert not gateway.invocations
        return
    assert state["policy_decision"].verdict == PolicyVerdict.ALLOW
    if withdraw == "before_execution":
        await world.w.graph_store.remove_node(identity)
    state = await execute_node(state, deps=deps)
    assert state["tool_result"].success is (withdraw is None)
    assert len(gateway.invocations) == (1 if withdraw is None else 0)


async def test_revised_path_can_reference_original_target_omitted_from_summary(world):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    world.llm.add(
        "path_review",
        {
            "decision": "revise",
            "reason": "Use the original measured target.",
            "path": _plan_for_register(path_draft(identity), world.runtime, context).model_dump(
                mode="json"
            ),
        },
    )
    world.llm.add("path_review", h.review_accept())
    accepted = await world.engine.register_path(
        _plan_for_register(path_draft(world.identities[0]), world.runtime, context),
        world.runtime,
        context,
        GraphObservation(),
    )
    assert accepted.steps[0].target_node_ids == [identity]
    assert len(world.llm.calls) == 2


async def test_forged_prompt_target_does_not_authorize_path(world):
    identity = "host:invented"
    context = await world.engine.context(world.runtime, GraphObservation())
    context["knowledge"]["known_targets"].append({"node_id": identity})
    with pytest.raises(ValueError, match="unknown target"):
        await world.engine.register_path(
            _plan_for_register(path_draft(identity), world.runtime, context),
            world.runtime,
            context,
            GraphObservation(),
        )
    assert world.llm.calls == []


@pytest.mark.parametrize(
    "change", ["summary_only", "original_outside_summary", "new_receipt_same_content"]
)
async def test_duplicate_execution_uses_original_facts_not_summary(world, change):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    path = await register(world, identity, context)
    first, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert not errors
    candidate = first[0]
    now = h.utcnow()
    await world.w.work.add(
        WorkItem(
            work_id="work-original-dedup",
            run_id=world.runtime.run_id,
            candidate_key=candidate.candidate_key,
            execution_identity_version=candidate.draft.execution_identity_version,
            agent_id=candidate.draft.agent_id,
            target_node_ids=candidate.draft.target_node_ids,
            priority=candidate.priority,
            status=WorkStatus.SUCCEEDED,
            created_at=now,
            updated_at=now,
        )
    )
    if change == "summary_only":
        context["knowledge"]["facts"] = []
    else:
        assert identity in context["knowledge"]["omitted_fact_node_ids"]
        await add_measured(
            world.w,
            "host-109",
            status_code=200 if change == "new_receipt_same_content" else 403,
            evidence_suffix="-new-measurement",
        )
        # Deliberately retain the earlier prompt: admission must read originals.
    second, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    if change != "original_outside_summary":
        assert second == []
        assert any("identical action already recorded" in error for error in errors)
        recorded = await world.w.work.list_items(run_id=world.runtime.run_id)
        assert len(recorded) == 1 and recorded[0].candidate_key == candidate.candidate_key
    else:
        assert second[0].candidate_key != candidate.candidate_key
        assert not errors


@pytest.mark.parametrize("legacy_status", [WorkStatus.PENDING, WorkStatus.SUCCEEDED])
@pytest.mark.parametrize("receipt", ["missing", "same", "different", "foreign"])
async def test_legacy_sqlite_execution_cannot_repeat_after_fingerprint_upgrade(
    world, tmp_path, legacy_status, receipt
):
    identity = world.identities[0]
    context = await world.engine.context(world.runtime, GraphObservation())
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert not errors
    candidate = compiled[0]
    old_identity = {
        "tool_id": str(candidate.tool.tool_id),
        "operation": candidate.plan_step.operation_key,
        "arguments": candidate.plan_step.arguments,
        "targets": list(map(str, candidate.draft.target_node_ids)),
        "principal_ref": candidate.plan_step.principal_ref,
        "knowledge": knowledge_fingerprint({"facts": context["knowledge"]["facts"]}),
    }
    legacy_key = (
        "cand_"
        + hashlib.sha256(
            json.dumps(old_identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    assert legacy_key != candidate.candidate_key
    filename = tmp_path / "legacy-work.sqlite"
    store = SQLiteWorkStore(filename)
    await store.add(
        WorkItem(
            work_id="legacy-executed",
            run_id=world.runtime.run_id,
            candidate_key=legacy_key,
            agent_id=candidate.draft.agent_id,
            target_node_ids=candidate.draft.target_node_ids,
            priority=candidate.priority,
            status=legacy_status,
            created_at=h.utcnow(),
            updated_at=h.utcnow(),
        )
    )
    if receipt != "missing":
        previous = candidate.plan_step.model_copy(
            update={"tool_id": "recon.other"} if receipt == "different" else {}
        )
        record = h.make_tool_evidence(
            evidence_id="legacy-receipt",
            run_id="other-run" if receipt == "foreign" else RUN,
            engagement_id=ENG,
            action_id="legacy-action",
            invocation_id="legacy-invocation",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=identity,
            tool_id=str(previous.tool_id),
        )
        record = record.model_copy(
            update={
                "metadata": {
                    **record.metadata,
                    "candidate_key": legacy_key,
                    "plan_step": previous.model_dump(mode="json"),
                }
            }
        )
        await world.w.evidence.append(record)
    world.engine.deps.work_store = SQLiteWorkStore(filename)
    _, errors = await world.engine.compile_actions(path, [action(identity)], world.runtime, context)
    if receipt == "different":
        assert not errors
    else:
        assert any("legacy execution identity requires reconciliation" in item for item in errors)
    # A legacy row for another target must not stop independent work.
    other = world.identities[1]
    other_path = await register(world, other, context)
    _, errors = await world.engine.compile_actions(
        other_path, [action(other)], world.runtime, context
    )
    assert not errors


async def test_scheduler_persists_original_identity_version_across_sqlite_restart(world, tmp_path):
    identity = world.identities[0]
    context = await world.engine.context(world.runtime, GraphObservation())
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert not errors
    filename = tmp_path / "new-work.sqlite"
    items = await WorkScheduler(SQLiteWorkStore(filename)).enqueue(
        compiled, graph_revision=1, run_id=world.runtime.run_id
    )
    reloaded = await SQLiteWorkStore(filename).get(items[0].work_id)
    assert reloaded.execution_identity_version == 2


@pytest.mark.parametrize("foreign_first", [False, True])
async def test_session_ownership_is_per_value_not_shared_host(world, foreign_first):
    service = AttackGraphService(world.w.graph_store)
    declarations = [(RUN, "current-session"), ("other-run", "foreign-session")]
    if foreign_first:
        declarations.reverse()
    for owner, reference in declarations:
        await service.upsert_node(
            GraphNodeType.HOST,
            key="shared-host",
            attrs={"in_scope": True},
            facts={"auth_session_config": {"sessions": [{"session_ref": reference}]}},
            provenance=Provenance(
                step=0,
                source=f"engagement:{ENG}",
                run_id=owner,
                confidence=Confidence.SERVER_DECLARED,
            ),
            step=0,
        )
    material = await collect_assessment(service, world.w.evidence, RUN, engagement_id=ENG)
    assert "foreign-session" not in material.principal_bindings
    if foreign_first:
        assert "current-session" in material.principal_bindings
    else:
        # The most recent nested claim is foreign; the older current value's
        # ownership was not persisted. Re-seeding repairs this ambiguity.
        assert "current-session" not in material.principal_bindings


async def test_seed_keeps_current_sessions_when_shared_host_is_reseeded(world):
    service = AttackGraphService(world.w.graph_store)
    declarations = [
        ("other-run", "foreign-session"),
        (RUN, "current-session"),
        (RUN, "second-session"),
        (RUN, "current-session"),
    ]
    for index, (owner, reference) in enumerate(declarations):
        sessions = [{"session_ref": reference, "paths": ["/"]}]
        if index == len(declarations) - 1:
            sessions.append({"session_ref": "second-session", "paths": ["/"]})
        engagement = h.make_engagement("shared-host").model_copy(
            update={"metadata": {"auth_sessions": {"shared-host": sessions}}}
        )
        await seed_from_engagement(service, engagement, run_id=owner)
    material = await collect_assessment(service, world.w.evidence, RUN, engagement_id=ENG)
    assert "foreign-session" not in material.principal_bindings
    assert {"current-session", "second-session"} <= set(material.principal_bindings)
    assert not any("auth_session_registrations" in ref for ref in material.fact_refs)
    # Retained ownership metadata does not keep a removed config usable.
    node = await service.get_node("host:shared-host")
    properties = {
        **node.properties,
        "facts": {**node.properties["facts"], "auth_session_config": {"sessions": []}},
    }
    await world.w.graph_store.remove_node(node.id)
    await world.w.graph_store.add_node(node.model_copy(update={"properties": properties}))
    material = await collect_assessment(service, world.w.evidence, RUN, engagement_id=ENG)
    assert set(material.principal_bindings) == {"anonymous"}


def progress_deps(world):
    return replace(
        build_test_application(
            graph_store=world.w.graph_store,
            evidence_store=world.w.evidence,
            tool_catalog=world.w.catalog,
        ),
        path_store=world.w.paths,
        task_graph_store=world.w.tasks,
        work_store=world.w.work,
    )


async def test_progress_and_resume_use_same_uncapped_original_content(world):
    deps = progress_deps(world)
    state = await observe_node(initial_graph_state(world.runtime), deps=deps)
    runner = AgentLoopRunner(deps)
    try:
        assert not await runner._canonical_graph_changed(state["runtime"])
        unchanged = await observe_node(state, deps=deps)
        assert unchanged["runtime"].no_progress_turns == 1
        await add_measured(world.w, "host-109", status_code=403, evidence_suffix="-changed")
        assert await runner._canonical_graph_changed(unchanged["runtime"])
        changed = await observe_node(unchanged, deps=deps)
        assert changed["runtime"].no_progress_turns == 0
        assert not await runner._canonical_graph_changed(changed["runtime"])
        # New receipt names for identical original facts are not progress.
        await add_measured(world.w, "host-109", status_code=403, evidence_suffix="-receipt")
        assert not await runner._canonical_graph_changed(changed["runtime"])
        repeated = await observe_node(changed, deps=deps)
        assert repeated["runtime"].no_progress_turns == 1
    finally:
        await runner.aclose()


async def test_failed_original_lookup_neither_wakes_nor_increments_stall(world):
    deps = progress_deps(world)
    state = await observe_node(initial_graph_state(world.runtime), deps=deps)

    class FailedEvidence:
        async def list_for_run(self, run_id):
            raise RuntimeError("unavailable")

    deps = replace(deps, evidence_store=FailedEvidence())
    runner = AgentLoopRunner(deps)
    try:
        assert not await runner._canonical_graph_changed(state["runtime"])
        failed = await observe_node(state, deps=deps)
        assert failed["runtime"].no_progress_turns == state["runtime"].no_progress_turns
        assert (
            failed["runtime"].last_progress_fingerprint
            == state["runtime"].last_progress_fingerprint
        )
    finally:
        await runner.aclose()


async def test_supported_conclusions_outside_summary_and_completed_steps_are_progress(world):
    service = AttackGraphService(world.w.graph_store)
    for index in range(60):
        await h.record_open_hypothesis(service, f"hyp-{index:03}", world.identities[0], RUN)
    proof = await world.w.evidence.get("proof-host-000")
    await world.w.evidence.append(proof.model_copy(update={"hypothesis_id": "hyp-059"}))
    deps = progress_deps(world)
    context = await world.engine.context(world.runtime, GraphObservation())
    assert "hyp-059" in context["knowledge"]["hypotheses"]["omitted_hypothesis_ids"]
    path = await register(world, world.identities[0], context)
    state = await observe_node(initial_graph_state(world.runtime), deps=deps)
    runner = AgentLoopRunner(deps)
    try:
        await service.resolve_hypothesis("hyp-059", status="confirmed", step=2)
        assert await runner._canonical_graph_changed(state["runtime"])
        state = await observe_node(state, deps=deps)
        assert state["runtime"].no_progress_turns == 0
        assert not await runner._canonical_graph_changed(state["runtime"])
        await world.w.paths.update_step(
            RUN,
            path.path_id,
            path.version,
            "step-1",
            "completed",
            outcome="observed",
            evidence_ids=["proof-host-000"],
        )
        assert await runner._canonical_graph_changed(state["runtime"])
        state = await observe_node(state, deps=deps)
        assert state["runtime"].no_progress_turns == 0
        assert not await runner._canonical_graph_changed(state["runtime"])
    finally:
        await runner.aclose()


@pytest.mark.parametrize("old", [None, "legacy-bounded-hash"])
async def test_incompatible_checkpoint_hash_does_not_prove_changed_originals(world, old):
    runner = AgentLoopRunner(progress_deps(world))
    try:
        runtime = world.runtime.model_copy(update={"last_progress_fingerprint": old})
        assert not await runner._canonical_graph_changed(runtime)
    finally:
        await runner.aclose()


@pytest.mark.parametrize("kind", ["inferred", "foreign_run", "foreign_engagement", "no_raw"])
async def test_summary_cannot_promote_unconfirmed_or_foreign_original_facts(world, kind):
    identity = await add_measured(
        world.w,
        "bad-host",
        run="other-run" if kind == "foreign_run" else RUN,
        engagement="other-eng" if kind == "foreign_engagement" else ENG,
        confidence=Confidence.INFERRED if kind == "inferred" else Confidence.MEASURED,
    )
    if kind == "no_raw":
        evidence = await world.w.evidence.get("proof-bad-host")
        await world.w.evidence.append(evidence.model_copy(update={"metadata": {}}))
    context = await world.engine.context(world.runtime, GraphObservation())
    context["knowledge"]["known_targets"].append({"node_id": identity})
    context["knowledge"]["fact_refs"].append(identity + "#fact:status_code")
    authority = await _collect_check_assessment(world.w.deps, world.runtime)
    assert identity not in authority.targets
    assert identity + "#fact:status_code" not in authority.fact_refs
    with pytest.raises(ValueError, match="unknown target"):
        await world.engine.register_path(
            _plan_for_register(path_draft(identity), world.runtime, context),
            world.runtime,
            context,
            GraphObservation(),
        )


@pytest.mark.parametrize("boundary", ["compile", "admission"])
async def test_original_fact_change_after_planning_blocks_execution(world, boundary):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert not errors
    node = await world.w.graph_store.get_node(identity)
    # Modify the actual value without measurement provenance for this new value.
    node.properties["facts"]["status_code"] = 403
    await world.w.graph_store.add_node(node)
    if boundary == "compile":
        compiled, errors = await world.engine.compile_actions(
            path, [action(identity)], world.runtime, context
        )
        assert errors
    else:
        errors = await validate_current_action(world.w.deps, world.runtime, compiled[0].plan_step)
        assert any("unconfirmed required fact" in error for error in errors)


async def test_declared_session_outside_prompt_cap_is_valid_but_not_a_measured_fact():
    llm = h.ScriptedLLM()
    w = h.make_world(llm)
    service = AttackGraphService(w.graph_store)
    for index in range(60):
        await service.upsert_node(
            GraphNodeType.HOST,
            key=f"provided-{index:03}",
            attrs={"in_scope": True},
            facts={"auth_session_config": {"sessions": [{"session_ref": f"session-{index}"}]}},
            labels=["engagement-scope"],
            provenance=Provenance(
                step=0,
                source=f"engagement:{ENG}",
                run_id=RUN,
                confidence=Confidence.SERVER_DECLARED,
            ),
            step=0,
        )
    summary = await project_knowledge(service, w.evidence, RUN)
    assert len(summary["provided"]["claims"]) == 50
    assert all(
        "session-59" not in claim["auth_session_refs"] for claim in summary["provided"]["claims"]
    )
    authority = await collect_assessment(service, w.evidence, RUN)
    assert authority.principal_bindings["session-59"] == {"session-59"}
    assert not authority.fact_refs
    runtime = h.make_runtime(RUN, ENG)
    engine = InvestigationEngine(w.deps)
    raw = h.draft_path("host:provided-059")
    raw["steps"][0]["principal_refs"] = ["session-59"]
    llm.add("path_review", h.review_accept())
    context = await engine.context(runtime, GraphObservation())
    path = await engine.register_path(
        _plan_for_register(raw, runtime, context), runtime, context, GraphObservation()
    )
    intent = action("host:provided-059").model_copy(
        update={"principal_ref": "session-59", "arguments": {"session_ref": "session-59"}}
    )
    compiled, errors = await engine.compile_actions(path, [intent], runtime, context)
    assert errors == []
    assert len(compiled) == 1
    assert await validate_current_action(w.deps, runtime, compiled[0].plan_step) == []


async def test_observation_and_evidence_refs_outside_per_node_caps_are_grounded():
    w = h.make_world(h.ScriptedLLM())
    identity = "host:observations"
    observations = []
    for index in range(25):
        eid = f"observation-{index:02}"
        await w.evidence.append(
            h.make_tool_evidence(
                evidence_id=eid,
                run_id=RUN,
                engagement_id=ENG,
                action_id=f"act-{index}",
                invocation_id=f"inv-{index}",
                path_id="observed",
                path_version=1,
                step_id="observe",
                target_node_id=identity,
            )
        )
        observations.append({"context_key": f"context-{index:02}", "evidence_id": eid})
    properties = build_properties(
        facts={
            "observations": observations,
            "evidence_ids": [item["evidence_id"] for item in observations],
        },
        provenance=Provenance(
            step=1,
            source="tool:inventory",
            run_id=RUN,
            evidence_id="observation-24",
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    await w.graph_store.add_node(
        GraphNode(
            id=identity,
            type=GraphNodeType.HOST,
            labels=[],
            properties=properties,
            created_at=h.utcnow(),
            updated_at=h.utcnow(),
        )
    )
    service = AttackGraphService(w.graph_store)
    summary = await project_knowledge(service, w.evidence, RUN)
    assert identity + "#obs:context-24" not in summary["fact_refs"]
    authority = await collect_assessment(service, w.evidence, RUN)
    assert identity + "#obs:context-24" in authority.fact_refs
    assert "evidence:observation-24" in authority.fact_refs


async def test_bare_foreign_evidence_reference_does_not_become_prerequisite(world):
    node = await world.w.graph_store.get_node(world.identities[0])
    node.properties["facts"]["evidence_ids"] = ["foreign-evidence"]
    await world.w.graph_store.add_node(node)
    authority = await _collect_check_assessment(world.w.deps, world.runtime)
    assert "evidence:foreign-evidence" not in authority.fact_refs


async def test_original_target_removed_during_model_review_cannot_be_registered(world):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    original = world.llm.complete_structured

    async def remove_after_review(**kwargs):
        response = await original(**kwargs)
        await world.w.graph_store.remove_node(identity)
        return response

    world.llm.complete_structured = remove_after_review
    with pytest.raises(ValueError, match="references changed"):
        await register(world, identity, context)
    assert await world.w.paths.get_active(RUN) is None


async def test_evidence_lookup_failure_does_not_fall_back_to_old_summary(world):
    identity = world.identities[-1]
    context = await world.engine.context(world.runtime, GraphObservation())
    path = await register(world, identity, context)
    compiled, errors = await world.engine.compile_actions(
        path, [action(identity)], world.runtime, context
    )
    assert errors == []

    async def unavailable(run):
        raise OSError("private transport details")

    world.w.evidence.list_for_run = unavailable
    errors = await validate_current_action(world.w.deps, world.runtime, compiled[0].plan_step)
    assert any("lookup failed" in error for error in errors)
    assert "private transport details" not in str(errors)


async def test_explicit_foreign_scope_registration_does_not_authorize_current_run():
    seed = await h.seed_host("other-run")
    w = h.make_world(h.ScriptedLLM(), graph_store=seed.store)
    authority = await collect_assessment(seed.service, w.evidence, RUN)
    assert seed.node_id not in authority.targets
