"""Public assembly/start with scripted judgments and synthetic native tools."""

import json
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.core.contracts.events import EventType
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import TASK_SETTLE_TOOL_ID
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_instance_driver_react import _PerConversationLLM
from tests.integration.test_native_react_loop import _engagement

PROFILE = Path(__file__).parents[2] / "config/agents/native-specialists.yaml"
WEB_PROFILE = PROFILE.with_name("native-specialists-web.yaml")


class ClosedLoopProvider(_PerConversationLLM):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.strategies = 0
        self.planning_inputs = []

    async def complete_structured(self, **kwargs):
        self.requests.append(("structured", kwargs["system"]))
        assert kwargs["num_retries"] == 0
        props = kwargs["response_schema"].get("properties", {})
        # Existing decision correction appends safe feedback after the JSON.
        payload, _ = json.JSONDecoder().raw_decode(kwargs["messages"][0]["content"])
        if "questions" in props:
            self.planning_inputs.append(payload)
            self.strategies += 1
            if self.strategies > 2:
                return {"questions": [], "reason": "No new grounded followup remains"}
            return {"questions": [{
                "question_id": f"q-{self.strategies}",
                "objective": f"Observe authorized inventory, independent pass {self.strategies}",
                "target_node_ids": ["host:a.local"],
                "completion_criteria": [{"kind": "semantic", "description":
                    f"Preserve an original inventory result for pass {self.strategies}"}],
            }], "reason": "Grounded bounded verification"}
        if "tasks" in props:
            question = payload["questions"][0]
            return {"tasks": [{
                "task_key": "atomic", "question_id": question["question_id"],
                "title": "Observe host", "objective": question["objective"],
                "role_id": "web-executor", "target_node_ids": question["target_node_ids"],
                "completion_criteria": question["completion_criteria"],
            }]}
        if "criterion_results" in props:
            receipts = payload["verified_source_ids"]
            return {**h.task_accept(receipts), "criterion_results": [{
                "criterion_id": condition["criterion_id"], "met": True,
                "evidence_ids": receipts, "reason": "Original synthetic inventory observed",
            } for condition in payload["conditions"]]}
        if "criteria" in props and "requested_criteria" in payload:
            receipts = [item["evidence_id"] for item in payload["evidence"]]
            tasks = [item["task_id"] for item in payload["tasks"]
                     if item["status"] == "completed"]
            return h.goal_accept([{
                "criterion": criterion, "met": True, "task_ids": tasks,
                "evidence_ids": receipts, "reason": "Both synthetic tasks completed",
            } for criterion in payload["requested_criteria"]])
        # Uses the same actual analysis/semantic reviewer schemas as dev.
        return await super().complete_structured(**kwargs)

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.requests.append(("native", system))
        assert kwargs["num_retries"] == 0
        self.native_turns += 1
        if self._settle_ok(messages, exposed_tool_name(TASK_SETTLE_TOOL_ID)):
            return NativeCompletion(
                tool_calls=(), text="Task settled; followup is a planner decision",
            )
        evidenced = self._executed_evidence(messages)
        if evidenced:
            return NativeCompletion(tool_calls=(NativeToolCall(
                call_id=f"settle-{self.native_turns}", name=exposed_tool_name(TASK_SETTLE_TOOL_ID),
                arguments={"outcome": "success", "reason": "Original inventory observed",
                           "evidence_ids": evidenced},
            ),))
        return NativeCompletion(tool_calls=(NativeToolCall(
            call_id=f"inventory-{self.native_turns}", name=exposed_tool_name(h.INVENTORY_TOOL),
            arguments={"host": "a.local"},
        ),))


async def assembled(
    tmp_path, provider, *, maximum=30, control=None, durable=False, real_control=False,
    profile=PROFILE,
):
    engagement = _engagement()
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement.model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(
        specialist_profile_path=str(profile),
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=maximum,
                                             max_concurrent_requests=2),
    ))
    if real_control:
        config.engagement.approval_policy.require_approval_for = ["recon"]
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()], output={"inventory": "ok"})
    settings = OrchestrationSettings.from_config(
        config.orchestration, max_steps=4, checkpointing=durable,
        checkpoint_backend="sqlite" if durable else "memory",
        checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
        task_graph_backend="sqlite" if durable else "memory",
        task_graph_path=str(tmp_path / "task-graph.sqlite3"),
        path_backend="sqlite" if durable else "memory",
        work_backend="sqlite" if durable else "memory",
        work_path=str(tmp_path / "work.sqlite3"),
        attack_graph_backend="sqlite" if durable else "memory",
        attack_graph_path=str(tmp_path / "attack-graph.sqlite3"),
        operator_intent_backend="sqlite" if durable else "memory",
        operator_intent_path=str(tmp_path / "operator-intents.sqlite3"),
    )
    engagement, deps = await build_application(
        config, llm=provider,
        control_plane=None if real_control else control or FakeControlPlane(), tool_gateway=gateway,
        tool_catalog=gateway, evidence_store=FakeEvidenceStore(),
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals", orchestration_settings=settings,
    )
    return engagement, deps, gateway


@pytest.mark.parametrize("profile", [PROFILE, WEB_PROFILE], ids=["full", "web-only"])
async def test_actual_start_planner_atomizer_native_review_followup_and_goal(
    tmp_path, monkeypatch, profile,
):
    monkeypatch.chdir(tmp_path)
    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider, profile=profile)
    if profile == WEB_PROFILE:
        assert deps.agents.agent_ids == ["web-executor"]
        assert deps.agents.definition("mobile-executor") is None
    final = await run_engagement(engagement, deps)
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert len(tasks) == 2  # no generic bootstrap task, no duplicate atomization
    assert all(task.status == TaskStatus.COMPLETED for task in tasks)
    assert all(task.completion_contract_version == 2 for task in tasks)
    assert len(gateway.invocations) == 2
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)

    async def instances(tx):
        return {row["instance_id"] for (kind, _), row in tx.records.items()
                if kind == "task_activation" and row["run_id"] == str(final.run_id)}

    assert len(await coordinator.change(instances)) == 2
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission.status == MissionStatus.COMPLETED
    ledger = RunRequestLedger(coordinator_for(deps.task_graph_store, deps.path_store))
    accounting = await ledger.snapshot(str(final.run_id))
    assert accounting["spent"] == len(provider.requests)
    assert accounting["reserved"] == 0
    assert all("Web execution worker" in system for kind, system in provider.requests
               if kind == "native")
    assert any("Investigation planner" in system for _, system in provider.requests)
    assert any("Atomic task designer" in system for _, system in provider.requests)
    assert any("Research and evidence reviewer" in system for _, system in provider.requests)
    followup = provider.planning_inputs[1]["related_task"]
    assert followup["status"] == "completed"
    assert followup["completion_evidence_id"] is not None
    assert followup["completion_contract_version"] == 2
    assert followup["completion_criteria"]


async def test_first_planner_counts_and_budget_cannot_spawn_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider, maximum=1)
    final = await run_engagement(engagement, deps)
    assert len(provider.requests) == 1
    assert gateway.invocations == []
    assert await deps.task_graph_store.list_tasks(mission_id=final.mission_id) == []
    assert final.mission_status != MissionStatus.COMPLETED


class InvalidAtomizationProvider(ClosedLoopProvider):
    def __init__(self, *, forever):
        super().__init__()
        self.forever, self.atomizations = forever, 0

    async def complete_structured(self, **kwargs):
        reply = await super().complete_structured(**kwargs)
        if "tasks" in kwargs["response_schema"].get("properties", {}):
            self.atomizations += 1
            if self.forever or self.atomizations == 1:
                reply["tasks"][0]["role_id"] = "unregistered-role"
        return reply


@pytest.mark.parametrize("forever", [False, True])
async def test_semantic_repair_is_finite_metered_and_never_admits_bad_tasks(
    tmp_path, monkeypatch, forever,
):
    monkeypatch.chdir(tmp_path)
    provider = InvalidAtomizationProvider(forever=forever)
    engagement, deps, gateway = await assembled(tmp_path, provider)
    final = await run_engagement(engagement, deps)
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    accounting = await RunRequestLedger(coordinator).snapshot(str(final.run_id))
    assert accounting["spent"] == len(provider.requests)
    tasks = await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    if not forever:
        assert len(tasks) == 2 and all(t.status == TaskStatus.COMPLETED for t in tasks)
        assert provider.atomizations == 3  # one correction plus the independent followup
        assert len(gateway.invocations) == 2
        return
    assert not tasks and not gateway.invocations
    assert provider.atomizations == 1 + deps.settings.decision_max_attempts
    assert final.planning_diagnostic.last_cause.code == "specialist_role_not_executable"
    assert not deps.event_bus.history(event_type=EventType.TASK_CREATED)

    async def attempts(tx):
        return [row for (kind, _), row in tx.records.items()
                if kind == "specialist_planning_attempt"]

    rows = await coordinator.change(attempts)
    assert len(rows) == 1 and rows[0]["state"] == "failed"
    assert rows[0]["diagnostic"]["last_cause"]["code"] == "specialist_role_not_executable"


async def test_graph_change_during_atomization_rejects_plan_without_erasing_original(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    class GraphChangingProvider(ClosedLoopProvider):
        async def complete_structured(self, **kwargs):
            reply = await super().complete_structured(**kwargs)
            if "tasks" in kwargs["response_schema"].get("properties", {}):
                node = await self.deps.graph_store.get_node("host:a.local")
                await self.deps.graph_store.add_node(node.model_copy(update={
                    "properties": {**node.properties, "observed_revision": "changed"},
                }))
            return reply

    provider = GraphChangingProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider)
    provider.deps = deps
    final = await run_engagement(engagement, deps)
    assert not gateway.invocations
    assert not await deps.task_graph_store.list_tasks(mission_id=final.mission_id)
    assert not deps.event_bus.history(event_type=EventType.TASK_CREATED)
    node = await deps.graph_store.get_node("host:a.local")
    assert node.properties["observed_revision"] == "changed"
    assert final.planning_diagnostic.code == "planning_context_changed"
    assert final.mission_status != MissionStatus.COMPLETED


async def test_held_reason_is_redacted_and_not_goal_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class HeldProvider(ClosedLoopProvider):
        async def complete_structured(self, **kwargs):
            reply = await super().complete_structured(**kwargs)
            if "questions" in kwargs["response_schema"].get("properties", {}):
                return {"questions": [], "reason": "Authorization: Bearer test-secret-token"}
            return reply

    provider = HeldProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider)
    final = await run_engagement(engagement, deps)
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)

    async def reasons(tx):
        return [row["reason"] for (kind, _), row in tx.records.items()
                if kind == "specialist_planning_attempt"]

    assert "test-secret-token" not in str(await coordinator.change(reasons))
    assert not gateway.invocations
    assert final.mission_status != MissionStatus.COMPLETED
