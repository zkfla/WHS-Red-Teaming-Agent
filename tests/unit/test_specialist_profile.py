"""Explicit fixture models and local stores: no live LLM/MCP/target calls."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from redteam.app.agent_profile import load_specialist_profile
from redteam.app.bootstrap import build_application
from redteam.app.runtime_components import binding_for_resume
from redteam.core.config import EngagementConfig, specialist_profile_from_environment
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient
from redteam.orchestration.component_binding import ComponentBindingError
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.nodes import aggregate_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.paths.models import PathValidationError
from redteam.orchestration.paths.writing import writing_schema
from redteam.orchestration.planning import BudgetedInference, PlanningRequest
from redteam.orchestration.specialist_planning import (
    AttackPathProposal,
    SerialSpecialistPlanningProvider,
    validate_atomization,
    validate_strategy,
)
from redteam.orchestration.state import initial_graph_state
from tests.fakes import DummyToolGateway, FakeControlPlane
from tests.integration import _engine_llm_harness as h

PROFILE = Path("config/agents/specialists.yaml")
WEB_ROLE = "web-execution-agent"


@pytest.fixture
def profile_path(tmp_path):
    shutil.copytree(PROFILE.parent, tmp_path / "agents")
    return tmp_path / "agents" / PROFILE.name


def _change_profile(path, update):
    raw = yaml.safe_load(path.read_text())
    update(raw)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


def _strategy(target="host:test"):
    return {"reason": "Observe the scoped service", "questions": [{
        "question_id": "q1", "objective": "Does the service answer?",
        "target_node_ids": [target],
        "completion_criteria": [{"kind": "evidence", "description": "Original response stored"}],
    }]}


def _atomized(target="host:test"):
    return {"path": {"reason": "Observe the scoped service", "steps": [{
        "step_id": "s1", "objective": "Observe the service response",
        "agent_id": WEB_ROLE, "target_node_ids": [target],
        "completion_criteria": [{"kind": "evidence", "description": "Original response stored"}],
    }]}, "origins": {"s1": "q1"}}


def test_profile_snapshot_exact_bytes_and_no_live_reload(profile_path):
    loaded = load_specialist_profile(profile_path)
    original = loaded.prompts.compose("next_action", "contract")
    same = load_specialist_profile(profile_path)
    assert loaded.fingerprint == same.fingerprint
    assert loaded.bindings == same.bindings
    path = profile_path.parent / "prompts" / "attack-path-planner.md"
    path.write_bytes(path.read_bytes() + b"\n")
    newer = load_specialist_profile(profile_path)
    assert loaded.fingerprint != newer.fingerprint
    assert loaded.prompts.compose("next_action", "contract") == original
    assert newer.bindings["attack-path-planner"]["sha256"] != (
        loaded.bindings["attack-path-planner"]["sha256"]
    )
    assert len(loaded.bindings) == 7
    with pytest.raises(ValueError, match="binding_missing"):
        loaded.prompts.compose("undeclared_stage", "")


@pytest.mark.parametrize("case", [
    "missing", "empty", "large", "escape", "symlink", "version", "bad_yaml", "bad_utf8",
])
def test_profile_rejects_invalid_instructions_before_runtime(profile_path, tmp_path, case):
    prompt = profile_path.parent / "prompts" / "conversation.md"
    if case == "missing":
        prompt.unlink()
    elif case == "empty":
        prompt.write_bytes(b" \n")
    elif case == "large":
        prompt.write_bytes(b"x" * 32769)
    elif case == "escape":
        _change_profile(profile_path, lambda raw: raw["roles"][0]["prompt"].update(
            path="../specialists.yaml"
        ))
    elif case == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text("external instructions")
        prompt.unlink()
        prompt.symlink_to(outside)
    elif case == "version":
        _change_profile(profile_path, lambda raw: raw["roles"][0]["prompt"].pop("version"))
    elif case == "bad_yaml":
        profile_path.write_bytes(b"roles: [")
    elif case == "bad_utf8":
        prompt.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        load_specialist_profile(profile_path)


def test_profile_is_not_a_closed_role_name_list(profile_path):
    def rename(raw):
        old = raw["roles"][3]["definition"]["agent_id"]
        raw["roles"][3]["definition"]["agent_id"] = "custom-web-role"
        raw["roles"][3]["definition"]["role"] = "custom-domain"
        raw["stages"] = {key: "custom-web-role" if val == old else val
                         for key, val in raw["stages"].items()}
    _change_profile(profile_path, rename)
    loaded = load_specialist_profile(profile_path)
    assert loaded.prompts.binding("worker_next_action", role_id="custom-web-role").role_id == (
        "custom-web-role"
    )


def test_profile_requires_task_review_binding_before_runtime(profile_path):
    _change_profile(profile_path, lambda raw: raw["stages"].pop("task_completion_review"))
    with pytest.raises(ValueError, match="specialist_profile_invalid"):
        load_specialist_profile(profile_path)


def test_configuration_does_not_read_other_environment(monkeypatch):
    monkeypatch.setenv("REDTEAM_SPECIALIST_PROFILE", "ambient.yaml")
    assert specialist_profile_from_environment({}) is None
    assert specialist_profile_from_environment({"REDTEAM_SPECIALIST_PROFILE": "chosen.yaml"}) == (
        "chosen.yaml"
    )
    with pytest.raises(ValueError, match="nonblank"):
        specialist_profile_from_environment({"REDTEAM_SPECIALIST_PROFILE": " "})


@pytest.mark.parametrize("case", ["unknown_target", "unknown_principal", "cycle"])
def test_strategy_fails_before_atomizer(case):
    raw = _strategy()
    if case == "unknown_target":
        raw["questions"][0]["target_node_ids"] = ["invented"]
    elif case == "unknown_principal":
        raw["questions"][0]["principal_refs"] = ["invented"]
    else:
        raw["questions"][0]["depends_on"] = ["q1"]
    with pytest.raises((ValueError, PathValidationError)):
        validate_strategy(AttackPathProposal.model_validate(raw), {
            "knowledge": {"known_targets": ["host:test"]},
        })


@pytest.mark.parametrize("case", ["origins", "subject", "criteria", "cycle", "dependency"])
def test_atomizer_cannot_drop_contracts(case):
    strategy = _strategy()
    atomic = _atomized()
    if case == "origins":
        atomic["origins"] = {}
    elif case == "subject":
        atomic["path"]["steps"][0]["target_node_ids"] = ["invented"]
    elif case == "criteria":
        atomic["path"]["steps"][0]["completion_criteria"] = []
    elif case == "cycle":
        atomic["path"]["steps"][0]["depends_on"] = ["s1"]
    else:
        question = deepcopy(strategy["questions"][0])
        question.update(question_id="q2", depends_on=["q1"])
        strategy["questions"].append(question)
        task = deepcopy(atomic["path"]["steps"][0])
        task["step_id"] = "s2"
        atomic["path"]["steps"].append(task)
        atomic["origins"]["s2"] = "q2"
    with pytest.raises((ValueError, PathValidationError)):
        validate_atomization(
            atomic["path"], AttackPathProposal.model_validate(strategy), atomic["origins"],
            max_tasks_per_question=6,
        )


class SpecialistLLM(h.ScriptedLLM):
    @staticmethod
    def _route_key(schema):
        props = schema.get("properties", {})
        if "questions" in props:
            return "attack_path_proposal"
        if "path" in props and "origins" in props:
            return "task_atomization"
        if "actions" in props and "decision" in props:
            return "worker_next_action"
        if "accepted" in props and "claims" in props:
            return "task_completion_review"
        return h.ScriptedLLM._route_key(schema)


async def test_planner_atomizer_share_the_same_budget_and_keep_provenance():
    llm = SpecialistLLM()
    llm.add("attack_path_proposal", _strategy())
    llm.add("task_atomization", _atomized())
    budget = DecisionBudget.reasoning_chain(8, 3)
    loaded = load_specialist_profile(PROFILE)
    request = PlanningRequest(
        stage="path_draft", payload={"knowledge": {"known_targets": ["host:test"]}},
        response_schema=writing_schema(PathPlan, selected_tool=True),
        inference=BudgetedInference(StructuredDecisionClient(llm), budget),
    )
    result = await SerialSpecialistPlanningProvider(loaded.prompts).propose(request)
    assert budget.used == 2
    assert [call["route"] for call in llm.calls] == [
        "attack_path_proposal", "task_atomization",
    ]
    provenance = result["planning_artifacts"]["specialist_decomposition"]
    assert provenance["origins"] == {"s1": "q1"}
    assert provenance["planner"] == loaded.bindings["attack-path-planner"]
    assert provenance["atomizer"] == loaded.bindings["task-atomizer"]
    assert llm.calls[0]["system"] != llm.calls[1]["system"]


async def test_invalid_subject_does_not_request_atomization():
    llm = SpecialistLLM()
    llm.add("attack_path_proposal", _strategy("invented"))
    loaded = load_specialist_profile(PROFILE)
    with pytest.raises(ValueError, match="unknown_target"):
        await SerialSpecialistPlanningProvider(loaded.prompts).propose(PlanningRequest(
            stage="path_draft", payload={"knowledge": {"known_targets": ["host:test"]}},
            response_schema=writing_schema(PathPlan, selected_tool=True),
            inference=BudgetedInference(StructuredDecisionClient(llm), DecisionBudget(3)),
        ))
    assert len(llm.calls) == 1


async def _application(tmp_path, llm, *, control=None):
    from redteam.core.config import AppConfig

    seed = await h.seed_host("run-specialist")
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()])
    config = AppConfig(
        engagement=EngagementConfig.model_validate(seed.engagement.model_dump()),
        specialist_profile=str(PROFILE),
    )
    engagement, deps = await build_application(
        config, llm=llm, tool_gateway=gateway, tool_catalog=gateway,
        attack_graph=seed.store, engagement_id=seed.engagement.engagement_id,
        control_plane=control, evidence_dir=tmp_path / "evidence",
        audit_dir=tmp_path / "audit", approval_dir=tmp_path / "approvals",
    )
    return seed, engagement, deps, gateway


@pytest.mark.parametrize("verdict", [
    PolicyVerdict.ALLOW, PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL,
])
async def test_serial_profile_uses_real_engine_worker_and_control_gate(tmp_path, verdict):
    llm = SpecialistLLM()
    control = FakeControlPlane(default_verdict=verdict)
    seed, engagement, deps, gateway = await _application(tmp_path, llm, control=control)
    assert deps.agent_prompts is not None
    assert set(deps.agents.agent_ids) == {WEB_ROLE, "mobile-execution-agent"}
    assert len(deps.agents.definition_ids) == 7
    runtime = h.make_runtime("run-specialist", str(engagement.engagement_id))
    engine = InvestigationEngine(deps)
    llm.add("attack_path_proposal", _strategy(seed.node_id))
    llm.add("task_atomization", _atomized(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", {"mode": "delegate", "reason": "Assign the observed purpose",
        "delegations": [{"path_step_id": "s1", "agent_id": WEB_ROLE,
                         "objective": "Does the service answer?", "budget": {"max_actions": 3}}]})
    from redteam.attack_graph.observation import GraphObservation

    path, actions, note = await engine.decide(runtime, GraphObservation())
    assert path is not None and not actions and note == "continue_planning"
    assert path.planning_artifacts["specialist_decomposition"]["version"] == "1"
    runtime = engine.updated_runtime
    assert runtime.model_requests_used == 4
    assert not gateway.invocations
    llm.add("worker_next_action", {"decision": "propose_actions", "actions": [{
        "target_node_id": seed.node_id, "tool_id": h.INVENTORY_TOOL,
        "arguments": {"host": seed.host}, "reason": "Observe the current scoped service",
    }], "reason": "One minimal observation"})
    planned = await plan_node(
        {**initial_graph_state(runtime), "observation": GraphObservation()}, deps=deps,
    )
    assert len(planned["dispatch_items"]) == 1
    item = planned["dispatch_items"][0]
    result = await worker_node(
        {"runtime": planned["runtime"], "dispatch_item": item}, deps=deps,
    )
    outcome = result["worker_outcomes"][str(item.proposal.action_id)]
    assert outcome.policy_verdict == verdict
    assert control.evaluated
    assert len(gateway.invocations) == (1 if verdict is PolicyVerdict.ALLOW else 0)
    assert outcome.executed == (verdict is PolicyVerdict.ALLOW)
    if verdict is PolicyVerdict.ALLOW:
        llm.add("analysis_verify", h.meaning("success", False))
        aggregated = await aggregate_node(
            {**planned, "worker_outcomes": result["worker_outcomes"]}, deps=deps,
        )
        assert aggregated["runtime"].actions_executed == 1
        records = await deps.evidence_store.list_for_run(runtime.run_id)
        assert any(record.source_type is EvidenceSourceType.TOOL for record in records)
    loaded = load_specialist_profile(PROFILE)
    system = next(call["system"] for call in llm.calls if call["route"] == "worker_next_action")
    assert loaded.prompts.binding("worker_next_action", role_id=WEB_ROLE).sha256 in (
        json.dumps(deps.component_binding.prompt_bindings)
    )
    assert "배정된 Web/API 원자 작업" in system


async def test_durable_binding_rejects_changed_prompt_and_accepts_same(profile_path, tmp_path):
    llm = SpecialistLLM()
    _, _, deps, _ = await _application(tmp_path, llm)
    loaded = load_specialist_profile(profile_path)
    binding = deps.component_binding.model_copy(update={
        "specialist_profile_path": loaded.path,
        "specialist_profile_fingerprint": loaded.fingerprint, "prompt_bindings": loaded.bindings,
    })
    assert binding_for_resume(binding) == binding
    path = profile_path.parent / "prompts" / "common.md"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ComponentBindingError, match="changed"):
        binding_for_resume(binding)


async def test_conversation_uses_trusted_snapshot_without_target_calls():
    from redteam.app.cli.conversation import ConversationAssistant

    llm = SpecialistLLM()
    llm.add("next_action", {"mode": "answer", "response": "아직 검증된 취약점이 없습니다."})
    loaded = load_specialist_profile(PROFILE)
    assistant = ConversationAssistant(llm, agent_prompts=loaded.prompts)
    decision = await assistant.decide(
        "현재 결과를 알려줘", engagement_name="test", scope=["test"],
    )
    assert decision.mode == "answer"
    assert len(llm.calls) == 1
    assert "사용자와 대화하는 동료 역할" in llm.calls[0]["system"]


async def test_planning_artifacts_survive_sqlite_restart_and_legacy_load(tmp_path):
    from redteam.orchestration.paths import SQLitePathStore

    loaded = load_specialist_profile(PROFILE)
    raw = h.draft_path("host:test")
    raw.update(path_id="path-db", run_id="run-db", version=1, status="active", goal="test")
    raw["planning_artifacts"] = {"specialist_decomposition": {
        "version": "1", "strategy": _strategy(), "origins": {"s1": "q1"},
        "planner": loaded.bindings["attack-path-planner"],
        "atomizer": loaded.bindings["task-atomizer"],
    }}
    path = PathPlan.model_validate(raw).model_copy(update={"status": "active"})
    file = tmp_path / "paths.sqlite3"
    await SQLitePathStore(file).register(path)
    restored = await SQLitePathStore(file).get_active(path.run_id)
    assert restored.planning_artifacts == path.planning_artifacts
    raw.pop("planning_artifacts")
    assert PathPlan.model_validate(raw).planning_artifacts == {}


async def test_report_projects_prompt_metadata_without_prompt_text(tmp_path):
    from redteam.attack_graph.service import AttackGraphService
    from redteam.core.enums import AgentPhase
    from redteam.orchestration.reporting import ReportingAgent

    _, engagement, deps, _ = await _application(tmp_path, SpecialistLLM())
    runtime = h.make_runtime("run-specialist", str(engagement.engagement_id)).model_copy(update={
        "phase": AgentPhase.FINISHED, "component_binding": deps.component_binding,
    })
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(deps.graph_store),
        evidence_store=deps.evidence_store, work_store=deps.work_store,
    )
    assert report.prompt_bindings == deps.component_binding.prompt_bindings
    assert report.specialist_profile_fingerprint == (
        deps.component_binding.specialist_profile_fingerprint
    )
    assert "당신은" not in report.model_dump_json()


async def test_profile_rejects_duplicate_selection_before_building_clients(tmp_path):
    from redteam.core.config import AppConfig
    from redteam.orchestration.agents.registry import default_registry

    seed = await h.seed_host("run-no-clients")
    config = AppConfig(
        engagement=EngagementConfig.model_validate(seed.engagement.model_dump()),
        specialist_profile=str(PROFILE),
    )
    llm = SpecialistLLM()
    with pytest.raises(ValueError, match="conflicts"):
        await build_application(config, llm=llm, agents=default_registry())
    assert llm.calls == []


async def test_validate_reports_bad_profile_without_model_or_tools(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from redteam.app.cli.commands.validate import run

    seed = await h.seed_host("run-validate")
    config = EngagementConfig.model_validate(seed.engagement.model_dump())
    source = tmp_path / "engagement.yaml"
    source.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "")
    monkeypatch.setenv("REDTEAM_SPECIALIST_PROFILE", str(tmp_path / "absent.yaml"))
    assert await run(SimpleNamespace(engagement_path=str(source), json=True)) == 2
    report = json.loads(capsys.readouterr().out)
    check = next(item for item in report["checks"] if item["name"] == "Specialist profile")
    assert not check["ok"]
