"""Registered code DATA, scripted judgments only; no generated-code execution."""

import asyncio
import json
from dataclasses import replace

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import TaskStatus
from redteam.evidence.artifacts import FilesystemArtifactBackend
from redteam.orchestration.code_artifacts import (
    KIND,
    inspect_artifact,
    prepare,
    register_code_artifacts,
)
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.semantic_completion import verify_single_origin
from redteam.orchestration.work.models import work_owner_for
from tests.integration.test_evidence_validity_consumers import bind, measured, measured_world
from tests.unit.test_native_specialist_profile import PROFILE

REQUEST = {"objective": "Compare supplied synthetic response objects",
           "source_evidence_ids": ["proof-race"]}


class AuthorReviewer:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.accepted = True
        self.wrong_digest = False
        self.source = "def evaluate(value):\n    return {'observed': bool(value)}\n"

    async def complete_structured(self, **kwargs):
        assert kwargs["num_retries"] == 0
        self.calls.append(kwargs)
        payload, _ = json.JSONDecoder().raw_decode(kwargs["messages"][0]["content"])
        if "source" in kwargs["response_schema"]["properties"]:
            return {"language": "python", "source": self.source,
                    "cases": [{"name": "present", "input": {"status": 200},
                               "expected": {"observed": True}}]}
        if self.hook is not None:
            await self.hook()
        return {"accepted": self.accepted,
                "artifact_digest": "0" * 64 if self.wrong_digest else payload["artifact_digest"],
                "reason": "Relevant synthetic-only comparison; execution not tested"}


async def configured(tmp_path, *, maximum=20, normal=4, repairs=1):
    world, runtime, path, _ = await measured_world()
    loaded = load_specialist_profile(PROFILE)
    deps = world.deps
    deps.specialist_policy = loaded.run_policy()
    deps.agent_prompts = loaded.prompts
    deps.artifact_store = FilesystemArtifactBackend(tmp_path / "blobs")
    runtime.current_agent = "web-executor"
    runtime.instance_id = "instance-code-test"
    task = await world.tasks.get_task(runtime.active_task_id)
    await world.tasks.update_task(task.model_copy(update={
        "assigned_agent": runtime.current_agent,
        "occupant_owner": work_owner_for(runtime.run_id, runtime.instance_id),
    }))
    admission = PlanningAdmission(deps.coordinator)
    await admission.bind_inputs(str(runtime.run_id), {"profile": loaded.fingerprint})
    ledger = RunRequestLedger(deps.coordinator)
    await ledger.bind(str(runtime.run_id), LLMRequestPolicy(mode="bounded", max_requests=maximum))
    provider = AuthorReviewer()
    deps.llm_client = RunMeteredLLM(provider, ledger, prompts=loaded.prompts)
    register_code_artifacts(deps.local_tools)
    context = LocalToolContext(deps, runtime, purpose=runtime.current_goal,
                               budget=DecisionBudget(normal_limit=normal, repair_limit=repairs))
    return world, runtime, path, provider, ledger, context


async def call(context, identity="code.prepare", arguments=None):
    with llm_request_scope(str(context.runtime.run_id),
                           work_owner_for(context.runtime.run_id, context.runtime.instance_id)):
        return await context.deps.local_tools.call(identity, arguments or REQUEST, context)


async def artifacts(world, runtime):
    return [r for r in await world.evidence.list_for_run(runtime.run_id)
            if r.metadata.get("record_kind") == KIND]


async def test_registered_prepare_independent_prompts_shared_accounting_and_durable_read(tmp_path):
    world, runtime, _, provider, ledger, context = await configured(tmp_path)
    before = (await world.evidence.get("proof-race")).model_dump()
    result = await call(context)
    assert result.ok, result.error
    assert result.runtime_outcome is None
    assert result.payload["fixture_validation"] == "not_run"
    assert result.payload["target_execution_enabled"] is False
    assert len(provider.calls) == context.budget.normal_used == 2
    assert "Reproduction artifact author" in provider.calls[0]["system"]
    assert "Research and evidence reviewer" in provider.calls[1]["system"]
    usage = await ledger.snapshot(str(runtime.run_id))
    assert usage["spent"] == usage["unknown_usage"] == 2 and usage["reserved"] == 0
    records = await artifacts(world, runtime)
    assert len(records) == 1 and records[0].digest == result.payload["artifact_digest"]
    assert (await world.evidence.get("proof-race")).model_dump() == before
    assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING
    # Reopen the existing immutable blob backend, not a process-local cache.
    world.deps.artifact_store = FilesystemArtifactBackend(tmp_path / "blobs")
    read = await call(context, "code.inspect", {"artifact_evidence_id": result.refs[0]})
    assert read.ok and read.payload == result.payload
    assert len(provider.calls) == 2
    replay = await call(context)
    assert replay.ok and replay.refs == result.refs
    assert len(await artifacts(world, runtime)) == 1


async def test_arbitrary_python_is_data_not_a_host_sandbox_claim(tmp_path):
    world, runtime, _, provider, _, context = await configured(tmp_path)
    marker = tmp_path / "must-not-exist"
    provider.source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('EXECUTED')\n"
    result = await call(context)
    assert result.ok and not marker.exists()
    assert result.payload["fixture_validation"] == "not_run"
    # SYSTEM artifact is never a preserved original target response.
    _, errors = await verify_single_origin(
        evidence_store=world.evidence, path_store=world.paths,
        run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
        mission_id=str(runtime.mission_id), evidence_id=result.refs[0],
        task_graph_store=world.tasks, work_store=world.work,
    )
    assert errors


@pytest.mark.parametrize("bad", ["missing", "failed", "other-run", "other-engagement",
                                  "fixture", "nested-fixture", "mock", "linkage", "stale"])
async def test_unproven_sources_rejected_before_model_or_artifact_write(tmp_path, bad):
    world, runtime, path, provider, _, context = await configured(tmp_path)
    original = await world.evidence.get("proof-race")
    request = dict(REQUEST)
    if bad == "missing":
        request["source_evidence_ids"] = ["does-not-exist"]
    elif bad == "failed":
        original.metadata["tool_result"]["success"] = False
    elif bad == "other-run":
        original.run_id = "another-run"
    elif bad == "other-engagement":
        original.engagement_id = "another-engagement"
    elif bad == "fixture":
        original.metadata["fixture_only"] = True
    elif bad == "nested-fixture":
        original.metadata["tool_result"]["metadata"]["fixture_only"] = True
    elif bad == "mock":
        original.metadata["mock"] = True
    elif bad == "linkage":
        original.metadata.pop("proposal")
    else:
        node = path.steps[0].target_node_ids[0]
        assert (await bind(world, runtime, node)).ok
        await measured(world, runtime, node, "app_version", "v2")
    result = await call(context, arguments=request)
    assert not result.ok
    assert not provider.calls and not await artifacts(world, runtime)


@pytest.mark.parametrize("kind", ["run", "caller", "denied", "digest", "syntax"])
async def test_generation_review_and_repairs_do_not_multiply_budgets(tmp_path, kind):
    world, runtime, _, provider, ledger, context = await configured(
        tmp_path, maximum=1 if kind == "run" else 20,
        normal=1 if kind == "caller" else 4, repairs=1,
    )
    if kind == "denied":
        provider.accepted = False
    elif kind == "digest":
        provider.wrong_digest = True
    elif kind == "syntax":
        provider.source = "def invalid(:"
    result = await call(context)
    assert not result.ok and not await artifacts(world, runtime)
    assert len(provider.calls) == {"run": 1, "caller": 1, "denied": 2,
                                   "digest": 3, "syntax": 2}[kind]
    assert (await ledger.snapshot(str(runtime.run_id)))["spent"] == len(provider.calls)
    assert context.budget.repair_used == int(kind in {"digest", "syntax"})


@pytest.mark.parametrize("change", ["goal", "task", "claim", "stop", "validity-aba",
                                     "source", "strategy", "unrelated"])
async def test_late_review_rechecks_relevant_input_and_claim_not_siblings(tmp_path, change):
    world, runtime, path, provider, _, context = await configured(tmp_path)
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok

    async def mutate():
        if change == "validity-aba":
            await measured(world, runtime, node, "app_version", "v2")
            await measured(world, runtime, node, "app_version", "v1")
        elif change == "unrelated":
            await measured(world, runtime, "host:sibling", "app_version", "v2")
        elif change == "source":
            (await world.evidence.get("proof-race")).summary = "corrupted original"
        else:
            async def update(tx):
                if change == "strategy":
                    tx.records[("strategy_revision", str(runtime.run_id))] = 8
                elif change == "stop":
                    tx.records[("binding", str(runtime.run_id))]["closed"] = True
                elif change == "goal":
                    mission = await tx.tasks.get_mission(runtime.mission_id)
                    await tx.tasks.update_mission(mission.model_copy(update={
                        "objective": "changed", "revision": mission.revision + 1,
                    }))
                else:
                    task = await tx.tasks.get_task(runtime.active_task_id)
                    await tx.tasks.update_task(task.model_copy(update={
                        "occupant_owner" if change == "claim" else "objective": "changed",
                    }))
            await world.deps.coordinator.change(update)
    provider.hook = mutate
    result = await call(context)
    assert result.ok is (change in {"strategy", "unrelated"}), result.error
    assert len(provider.calls) == 2
    assert bool(await artifacts(world, runtime)) is result.ok


@pytest.mark.parametrize("bad", ["blob", "metadata", "prompt", "task", "scope"])
async def test_inspection_rejects_corruption_and_stale_current_bindings(tmp_path, bad):
    world, runtime, _, provider, _, context = await configured(tmp_path)
    result = await call(context)
    assert result.ok
    record = (await artifacts(world, runtime))[0]
    if bad == "blob":
        class CorruptRead:
            async def get(self, ref):
                return b"{}"
        world.deps.artifact_store = CorruptRead()
    elif bad == "metadata":
        record.metadata["review"]["accepted"] = False
    elif bad == "prompt":
        class ChangedPrompt:
            def binding(self, stage):
                return world.original_prompts.binding(stage).model_copy(update={"sha256": "f" * 64})
        world.original_prompts = world.deps.agent_prompts
        world.deps.agent_prompts = ChangedPrompt()
    elif bad == "task":
        task = await world.tasks.get_task(runtime.active_task_id)
        await world.tasks.update_task(task.model_copy(update={"objective": "new purpose"}))
    else:
        runtime.instance_id = "another-instance"
    read = await call(context, "code.inspect", {"artifact_evidence_id": result.refs[0]})
    assert not read.ok and len(provider.calls) == 2


async def test_stop_during_blob_publish_cannot_admit_artifact(tmp_path):
    world, runtime, _, _, _, context = await configured(tmp_path)
    backend = world.deps.artifact_store

    class StopOnPublish:
        async def put(self, data, content_type):
            blob = await backend.put(data, content_type)
            async def stop(tx):
                tx.records[("binding", str(runtime.run_id))]["closed"] = True
            await world.deps.coordinator.change(stop)
            return blob

        async def verify(self, ref, digest):
            return await backend.verify(ref, digest)

    world.deps.artifact_store = StopOnPublish()
    result = await call(context)
    assert not result.ok and not await artifacts(world, runtime)
    assert list((tmp_path / "blobs").glob("artifact_*.bin"))  # historical, not admitted


async def test_cancellation_keeps_dispatched_request_charged(tmp_path):
    world, runtime, _, provider, ledger, context = await configured(tmp_path)

    async def cancel():
        raise asyncio.CancelledError

    provider.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        await call(context)
    usage = await ledger.snapshot(str(runtime.run_id))
    assert usage["spent"] == 2 and usage["unknown_dispatch"] == 1
    assert not await artifacts(world, runtime)


async def test_handlers_require_explicit_configuration_and_requester_budget(tmp_path):
    world, _, _, provider, _, context = await configured(tmp_path)
    unbudgeted = replace(context, budget=None)
    assert not (await prepare(REQUEST, unbudgeted)).ok
    world.deps.agent_prompts = None
    assert not (await prepare(REQUEST, context)).ok
    assert not (await inspect_artifact({"artifact_evidence_id": "unknown"}, context)).ok
    assert not provider.calls


async def test_public_generic_dispatch_discovers_code_schema_and_accounts_requester(tmp_path):
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.tool_discovery import tool_get_detail_handler

    world, runtime, path, provider, ledger, context = await configured(tmp_path)
    detail = await tool_get_detail_handler({"tool_id": "code.prepare"}, context)
    assert detail.ok
    assert set(detail.payload["required_arguments"]) == {"objective", "source_evidence_ids"}
    engine = InvestigationEngine(world.deps)
    budget = engine.begin_native_turn(runtime)
    with llm_request_scope(str(runtime.run_id),
                           work_owner_for(runtime.run_id, runtime.instance_id)):
        results = await engine.execute_local_calls(
            [{"tool_id": "code.prepare", "arguments": REQUEST}],
            runtime=runtime, path=path, budget=budget,
        )
    assert results[0]["result"]["ok"], results
    assert len(provider.calls) == runtime.model_requests_used == 2
    assert (await ledger.snapshot(str(runtime.run_id)))["spent"] == 2
    assert results[0]["result"]["runtime_outcome"] is None
    assert len(runtime.local_tool_result_ids) == 1
    assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


async def test_public_assembly_wires_existing_blob_port_only_for_selected_feature(tmp_path):
    from redteam.app.bootstrap import build_application
    from redteam.core.config import AppConfig, EngagementConfig
    from tests.fakes import FakeControlPlane, FakeEvidenceStore
    from tests.integration.test_native_specialist_closed_loop import assembled

    engagement, deps, gateway = await assembled(tmp_path / "selected", AuthorReviewer())
    assert isinstance(deps.artifact_store, FilesystemArtifactBackend)
    assert deps.agent_prompts is not None
    assert deps.local_tools.get("code.prepare") and deps.local_tools.get("code.inspect")
    assert deps.agents.definition("code-author").schedulable is False
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    _, generic = await build_application(
        config, llm=AuthorReviewer(), tool_gateway=gateway, tool_catalog=gateway,
        control_plane=FakeControlPlane(), evidence_store=FakeEvidenceStore(),
        evidence_dir=tmp_path / "generic/evidence", audit_dir=tmp_path / "generic/audit",
        approval_dir=tmp_path / "generic/approvals",
    )
    assert generic.artifact_store is None and generic.agent_prompts is None
    assert generic.local_tools.get("code.prepare") is None
    assert not (tmp_path / "generic/evidence/artifacts").exists()
