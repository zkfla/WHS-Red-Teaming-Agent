"""Scripted model/supervisor/network double; real catalog, adapter, Control and SQLite.

No Docker/target/paid-model calls. Simulated gate observations prove propagation
and holder binding only; they are not physical network or vulnerability proof.
"""

import json
import shutil
from functools import partial

import pytest

from redteam.app.central_run import open_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gated_manifest import build_gated_descriptors
from redteam.capability.gated_strategy import GatedExecutionPolicy
from redteam.capability.gated_window import GATE_SOURCE_ID, GatedWindowExtension
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import SHELL_SERVER_ID, build_shell_descriptors
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult, ShellWorkroomManager
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.network import MeterReading, NetworkGrant
from redteam.core.enums import ApprovalStatus, EvidenceSourceType
from redteam.core.ports.program_discovery import ProgramDiscoveryError
from redteam.core.scope_rules import check_target_scope
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.tool_discovery import NATIVE_INPUT_LIMIT
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_development import HOST, call, turn
from tests.integration.test_central_parallel import limits
from tests.integration.test_probe_workers import PROFILE, drive, register
from tests.unit.test_gated_window import _policy
from tests.unit.test_tool_discovery import _row

TARGET_TOOL = "analysis.shell_target_run"
SCRIPT = "/usr/bin/curl --max-time 2 http://10.0.0.9:8080/"


class ScriptedWorkroom(ShellWorkroomManager):
    def __init__(self):
        super().__init__(ShellWorkroomPolicy(image="sha256:" + "a" * 64))
        self.executions = []

    async def list_image_programs(self, manifest_path=None):
        return [_row("curl"), *[_row(f"fixture{i:04d}") for i in range(500)]]

    async def list_instance_programs(self, **kwargs):
        raise ProgramDiscoveryError("shell_workroom_not_running")

    async def exec_script(self, key, language, script, timeout_seconds=None, **kwargs):
        self.executions.append((key, language, script, kwargs))
        return ExecResult(
            exit_code=7, stdout="program-observation " + "x" * 9000,
            stderr="connection refused", timed_out=False, truncated=True,
            duration_ms=3, workroom_key=key, container_name="fixture-only",
        )

    async def _run_docker(self, *args, **kwargs):
        raise AssertionError("this test must not execute Docker")


class ScriptedWindow(GatedWindowExtension):
    async def open_window(self, **kwargs):
        self.fixture_open = kwargs
        grant = await self.ledger.request_window(**{
            key: kwargs[key] for key in ("run_id", "instance_id", "epoch", "caps")
        })
        assert isinstance(grant, NetworkGrant)
        self.fixture_grant = grant
        return grant

    async def close_window(self, key):
        grant, args = self.fixture_grant, self.fixture_open
        settled = await self.ledger.settle_window(
            window_id=grant.window_id,
            meter=MeterReading(metered_packets=0, metered_bytes=0), verified_closed=True,
        )
        self._companions[grant.window_id] = {
            "source_id": GATE_SOURCE_ID, "window_id": grant.window_id,
            "run_id": args["run_id"], "instance_id": args["instance_id"],
            "epoch": args["epoch"], "outcome": settled.outcome,
            "holder": {"action_id": args["holder_action_id"],
                       "invocation_id": args["holder_invocation_id"]},
            "scope": [{"ip": flow.ip, "port": flow.port, "proto": flow.proto}
                      for flow in args["allowed"]], "admissions": [],
        }
        return settled


class SchemaSensitiveProgramProbe(FakeLLMClient):
    def __init__(self, *, extra_read=False):
        super().__init__()
        self.envelopes, self.planner_inputs = [], []
        self.extra_read, self.read_issued = extra_read, False

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.envelopes.append({"messages": messages, "tools": tools, "system": system})
        payload = worker_input(messages)
        originals = payload["probe"]["original_evidence_ids"]
        if originals and self.extra_read and not self.read_issued:
            self.read_issued = True
            response = turn(call("read-original", "evidence.read", {
                "evidence_id": originals[0], "limit": 500,
            }))
        elif originals:
            response = turn(call("return", "task.settle", {
                "outcome": "inconclusive", "reason": "Program failed; preserve observation",
                "evidence_ids": originals,
            }))
        elif exposed_tool_name(TARGET_TOOL) in {spec.name for spec in tools}:
            route = next(item for item in payload["program_execution_environment"]["routes"]
                         if item["purpose"] == "target")
            assert route["status"] == "target_ready" and TARGET_TOOL in route["tool_ids"]
            response = turn(call("program", TARGET_TOOL, {
                "language": "bash", "script": SCRIPT, "target_ref": HOST, "port": 8080,
            }))
        else:
            response = turn(call("detail", "tool.get_detail", {"tool_id": TARGET_TOOL}))
        self.native_responses = [response]
        return await super().complete_native(messages, tools, system=system, **kwargs)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "updates" in response_schema["properties"]:
            self.planner_inputs.append(json.loads(messages[-1]["content"]))
            self._canned_structured = {
                "updates": [], "questions": [],
                "reason": "Need a reachable target before further testing", "termination": "held",
                "resume_conditions": ["Target responds on the selected route"],
            }
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


@pytest.mark.parametrize("approval,extra_read", [(False, False), (True, False), (False, True)])
async def test_program_original_reaches_worker_and_planner_with_one_original_execution(
    tmp_path, approval, extra_read,
):
    original, settings = limits()
    original.metadata["success_criteria"] = ["Inspect authorized target response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    settings = settings.with_overrides(specialist_profile_path=str(PROFILE))
    manager = ScriptedWorkroom()
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    descriptors = [*build_shell_descriptors(manager.policy),
                   *build_gated_descriptors(manager.policy)]
    if approval:
        from redteam.core.enums import RiskLevel
        descriptors = [item.model_copy(update={"risk_level": RiskLevel.HIGH})
                       if str(item.tool_id) == TARGET_TOOL else item for item in descriptors]
    registry = MCPToolRegistry()
    for item in descriptors:
        registry.register(item)
    catalog = MCPToolCatalog(registry)
    gateway = MCPToolGateway(registry, {SHELL_SERVER_ID: adapter}, health_refresh_enabled=False)
    llm = SchemaSensitiveProgramProbe(extra_read=extra_read)
    async with open_central_run(
        config, directory=tmp_path / "run", settings=settings, llm=llm,
        tool_gateway=gateway, tool_catalog=catalog, shell_programs=manager,
    ) as session:
        policy = _policy().model_copy(update={
            "registry": _policy().registry.model_copy(update={"targets": {HOST: ("10.0.0.9",)}}),
            "target_ports": {HOST: (8080,)},
        })
        ledger = SQLiteNetworkGrantLedger(
            tmp_path / "network.sqlite3", engagement_id=str(session.runtime.engagement_id),
            budget=policy.budget,
        )
        await ledger.setup()
        extension = ScriptedWindow(
            manager=manager, ledger=ledger, run_docker=manager._run_docker, policy=policy,
            target_route_identity={"connection": "daemon_routed"},
            docker_endpoint={"context": "fixture", "host": "unix:///fixture"},
        )
        manager.set_execution_admission(extension)
        executor = GatedExecutionPolicy(
            extension, scope_validator=partial(
                check_target_scope, scope=session.deps.engagement.scope,
            ),
            evidence_store=session.deps.evidence_store,
        )
        adapter.set_gated_policy(executor, descriptors)
        identity = await register(session, "program-observation", actions=2)
        outcome, = await drive(session)
        if approval:
            assert not manager.executions and len(llm.envelopes) == 1
            execution, = await session.deps.central_state.list_executions()
            await session.deps.control_plane.resolve_approval(
                outcome.runtime.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
            )
            recovered, recovery = await session.driver.recover_central_instance(outcome.runtime)
            assert recovery.ready
            await session.driver.runner_for(recovered).resume_recovered(recovered)
            assert manager.executions[0][3]["invocation_id"] == str(execution.invocation_id)
        assert len(manager.executions) == 1
        assert manager.executions[0][1:3] == ("bash", SCRIPT)
        task = await session.deps.central_state.get_task(identity)
        assert task.result.outcome == "inconclusive"
        records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
        raw, = [item for item in records if item.source_type is EvidenceSourceType.TOOL]
        assert raw.metadata["tool_result"]["output"]["exit_code"] == 7
        assert raw.metadata["tool_result"]["metadata"]["gated_evidence_id"]
        if extra_read:
            read_result, = [item for item in records
                           if item.metadata.get("tool_id") == "evidence.read"
                           and item.metadata.get("record_kind") == "runtime_tool_result"]
            retained = read_result.metadata["result"]["payload"]
            assert retained["program_output"]["stderr"] == "connection refused"
            assert retained["text"] and retained["limit"] == 500
        tool_bodies = [json.loads(item["content"]) for item in llm.envelopes[-1]["messages"]
                       if item["role"] == "tool"]
        worker, = [view for body in tool_bodies for view in body.get("original_results", [])]
        assert worker["program_output"]["stderr"] == "connection refused"
        assert worker["execution"]["accounting"]["exit_code"] == 7
        assert worker["execution"]["gate_companion"]["status"] == "linked"
        await SpecialistPlanner(session.deps).advance(session.runtime)
        planner = llm.planner_inputs[-1]
        view, = planner["original_observations"]["records"]
        assert view["execution"] == worker["execution"]
        preview = view["program_output"]
        original_output = worker["program_output"]
        assert original_output["stdout"].startswith("program-observation")
        assert preview["stdout"].startswith("program-observation")
        assert preview["stdout"].startswith(original_output["stdout"])
        assert preview["stderr"] == original_output["stderr"]
        assert len(preview["stdout"]) + len(preview["stderr"]) <= 2000
        assert preview["stdout_truncated"] or preview["stderr_truncated"]
        assert view["provenance"]["invocation_id"] == str(raw.invocation_id)
        assert planner["program_execution_environment"]["routes"][1]["status"] == "target_ready"
        assert planner["tool_cards_truncated"]
        assert len(llm.envelopes) == 2 + int(extra_read)
        assert all(NATIVE_INPUT_LIMIT.fits(**envelope) for envelope in llm.envelopes)
        usage = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        assert usage["spent"] == len(llm.requests) == 3 + int(extra_read)
        assert usage["known_tokens"] == 20 + 10 * int(extra_read) and usage["unknown_usage"] == 1
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_changed_worker_prompt_rejects_original_run_resume_before_any_model_call(tmp_path):
    from tests.integration.test_native_schema_delivery import provider

    shutil.copytree(PROFILE.parent, tmp_path / "agents")
    selected_profile = tmp_path / "agents" / PROFILE.name
    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(selected_profile))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    tools, llm = provider(), FakeLLMClient()
    kwargs = {"directory": tmp_path / "run", "settings": settings, "llm": llm,
              "tool_gateway": tools, "tool_catalog": tools.catalog}
    async with open_central_run(config, **kwargs) as session:
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
    prompt = selected_profile.parent / "prompts/worker.md"
    prompt.write_text(
        prompt.read_text(encoding="utf-8") + "\nChanged trusted execution instructions.\n",
        encoding="utf-8",
    )
    with pytest.raises(StateConflict, match="run_binding_changed_new_run_required"):
        async with open_central_run(
            config, resume=True, run_id=run_id, engagement_id=engagement_id, **kwargs,
        ):
            pytest.fail("changed prompt must not resume the original Run")
    assert not llm.requests and not tools.inner.invocations
