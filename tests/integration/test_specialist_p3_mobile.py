"""P3 preparation: fixture model/ADB, real stdio MCP/Control/evidence pipeline."""

import json
import sys
from contextlib import AsyncExitStack

import pytest

from redteam.app.bootstrap import build_application
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.mobile_deployment import (
    MobileDeploymentSpec,
    connection_environment,
    provision,
)
from redteam.core.config import AppConfig, CapabilityConfig, load_engagement_config
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import RunId
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.nodes.aggregate import aggregate_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import initial_graph_state
from tests.integration import _engine_llm_harness as h
from tests.unit.test_mobile_deployment import engagement as engagement_fixture
from tests.unit.test_specialist_profile import SpecialistLLM, _atomized, _strategy


@pytest.mark.parametrize("policy", ["allow", "approval", "deny", "mismatch"])
async def test_specialist_mobile_uses_generated_binding_and_existing_control(
    tmp_path,
    monkeypatch,
    policy,
):
    calls = tmp_path / "synthetic-adb-calls.json"
    adb = tmp_path / "synthetic-adb"
    # Explicit test double executable: no real Android device/app is used.
    adb.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('[ro.product.model]: [synthetic fixture]')\n",
    )
    adb.chmod(0o700)
    spec = MobileDeploymentSpec(
        server_id="synthetic-mobile",
        adb_path=str(adb),
        devices={
            "test-device": {
                "asset_ref": "test-app",
                "serial": "synthetic-serial",
                "package_name": "org.test.app",
            },
        },
        allowed_commands=["getprop"],
    )
    config = engagement_fixture()
    if policy == "approval":
        config.approval_policy.require_approval_for = ["mobile.device_shell"]
    if policy == "deny":
        from redteam.core.contracts.engagement import RuleOfEngagement

        config.rules_of_engagement = [
            RuleOfEngagement(name="no-mobile", denied_capabilities=["mobile"])
        ]
    prepared = provision(spec, config, tmp_path / "generated")
    loaded = load_engagement_config(prepared)
    binding = loaded.metadata["mobile_deployment"]
    for key, value in connection_environment(
        spec,
        binding["policy_path"],
        binding["policy_sha256"],
    ).items():
        monkeypatch.setenv(key, value)
    app = AppConfig(
        engagement=loaded,
        capability=CapabilityConfig(mcp_servers=[spec.server_id]),
        specialist_profile="config/agents/specialists-p2.yaml",
    )
    llm = SpecialistLLM()
    async with AsyncExitStack() as stack:
        eng, deps = await build_application(
            app,
            llm=llm,
            mcp_stack=stack,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
        runtime = h.make_runtime("run-mobile-specialist", str(eng.engagement_id))
        nodes = await seed_from_engagement(
            AttackGraphService(deps.graph_store),
            eng,
            run_id=RunId(runtime.run_id),
            step=0,
        )
        target = str(nodes[0].id)
        strategy, atomized = _strategy(target), _atomized(target)
        atomized["path"]["steps"][0]["agent_id"] = "mobile-execution-agent"
        llm.add("attack_path_proposal", strategy)
        llm.add("task_atomization", atomized)
        llm.add("path_review", h.review_accept())
        llm.add(
            "next_action",
            {
                "mode": "delegate",
                "reason": "Bounded mobile diagnostic",
                "delegations": [
                    {
                        "path_step_id": "s1",
                        "agent_id": "mobile-execution-agent",
                        "budget": {"max_actions": 1},
                    }
                ],
            },
        )
        engine = InvestigationEngine(deps)
        path, actions, note = await engine.decide(runtime, GraphObservation())
        assert path is not None and not actions and note == "continue_planning"
        tool = deps.tool_catalog.get("mobile.device_shell")
        assert tool is not None
        assert set(tool.required_arguments) == {"device_ref", "command"}
        assert tool.target_types == ["device"]
        assert tool.argument_bindings == {"device_ref": "device_ref"}
        assert tool.command_fields == ["command"]
        assert tool.output_schema and "truncated" in tool.output_schema["properties"]
        llm.add(
            "worker_next_action",
            {
                "decision": "propose_actions",
                "reason": "One fixed diagnostic",
                "actions": [
                    {
                        "target_node_id": target,
                        "tool_id": str(tool.tool_id),
                        "arguments": {
                            "command": "getprop",
                            **({"device_ref": "foreign-device"} if policy == "mismatch" else {}),
                        },
                        "reason": "Observe configured device",
                    }
                ],
            },
        )
        planned = await plan_node(
            {**initial_graph_state(engine.updated_runtime), "observation": GraphObservation()},
            deps=deps,
        )
        if policy == "mismatch":
            assert planned["dispatch_items"] == []
            assert not calls.exists()
            return
        assert len(planned["dispatch_items"]) == 1, planned["runtime"].planning_feedback
        item = planned["dispatch_items"][0]
        assert item.proposal.target.device_ref == "test-device"
        assert item.proposal.target.package_name == "org.test.app"
        assert item.proposal.arguments == {"device_ref": "test-device", "command": "getprop"}
        assert not calls.exists()  # Discovery/planning are not device access.
        llm.add("analysis_verify", h.meaning("inconclusive", False))
        result = await worker_node(
            {"runtime": planned["runtime"], "dispatch_item": item}, deps=deps
        )
        outcome = result["worker_outcomes"][str(item.proposal.action_id)]
        verdict = {
            "allow": PolicyVerdict.ALLOW,
            "approval": PolicyVerdict.REQUIRE_APPROVAL,
            "deny": PolicyVerdict.DENY,
        }[policy]
        assert outcome.policy_verdict is verdict
        assert outcome.executed is (policy == "allow")
        if policy == "allow":
            assert outcome.succeeded
            assert json.loads(calls.read_text()) == ["-s", "synthetic-serial", "shell", "getprop"]
            aggregate = await aggregate_node(
                {**planned, "worker_outcomes": result["worker_outcomes"]},
                deps=deps,
            )
            assert aggregate["runtime"].actions_executed == 1
            evidence = await deps.evidence_store.list_for_run(runtime.run_id)
            originals = [e for e in evidence if e.source_type is EvidenceSourceType.TOOL]
            assert len(originals) == 1
            assert originals[0].metadata["success"] is True
            assert not (await AttackGraphService(deps.graph_store).observe()).findings
        else:
            assert not calls.exists()
