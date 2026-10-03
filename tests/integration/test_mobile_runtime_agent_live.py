"""Opt-in Mobile Agent engagement against mobile-next and a real emulator."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.mobile_analysis_manifest import MOBILE_ANALYSIS_TOOLS
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventType
from redteam.core.enums import GraphNodeType
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents import (
    MOBILE_AGENT,
    AgentRegistry,
    builtin_agent_definitions,
)
from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES, CANDIDATE_REJECTED
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason

_DEVICE_SERIAL_ENV = "REDTEAM_TEST_MOBILE_E2E_DEVICE_SERIAL"
_DEVICE_REF_ENV = "REDTEAM_TEST_MOBILE_E2E_DEVICE_REF"
_PACKAGE_ENV = "REDTEAM_TEST_MOBILE_E2E_PACKAGE"
_DEVICE_SERIAL = os.environ.get(_DEVICE_SERIAL_ENV)
_DEVICE_REF = os.environ.get(_DEVICE_REF_ENV)
_PACKAGE = os.environ.get(_PACKAGE_ENV, "com.android.insecurebankv2")
_EXPECT_CRASH = os.environ.get("REDTEAM_TEST_MOBILE_E2E_EXPECT_CRASH") == "1"


@pytest.mark.skipif(
    not _DEVICE_SERIAL or not _DEVICE_REF,
    reason=f"set {_DEVICE_SERIAL_ENV} and {_DEVICE_REF_ENV} to an authorized device",
)
async def test_real_mobile_next_observes_runtime_and_maps_a_crash_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    app_path = repository_root / "deploy" / "mcp" / "mobile-analysis" / "app.yaml"
    specs = MOBILE_ANALYSIS_TOOLS["mobile-next"]
    tool_names = [spec.tool_id.partition(".")[2] for spec in specs]
    operation_bindings = {
        spec.tool_id.partition(".")[2]: [spec.operation] for spec in specs
    }
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    assert npx is not None

    android_sdk = os.environ.get("ANDROID_HOME", r"D:\AndroidSdk")
    monkeypatch.setenv("ANDROID_HOME", android_sdk)
    monkeypatch.setenv("ANDROID_SDK_ROOT", android_sdk)
    monkeypatch.setenv("MOBILEMCP_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_NEXT_KIND", "mobile")
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_NEXT_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_NEXT_COMMAND", npx)
    monkeypatch.setenv(
        "REDTEAM_MCP_MOBILE_NEXT_ARGS_JSON",
        json.dumps(["-y", "@mobilenext/mobile-mcp@1.0.4"]),
    )
    monkeypatch.setenv(
        "REDTEAM_MCP_MOBILE_NEXT_PASSTHROUGH_ENV",
        "ANDROID_HOME,ANDROID_SDK_ROOT,PATH,MOBILEMCP_DISABLE_TELEMETRY",
    )
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_NEXT_RISK_LEVEL", "medium")
    monkeypatch.setenv(
        "REDTEAM_MCP_MOBILE_NEXT_ALLOWED_TOOLS_JSON",
        json.dumps(tool_names),
    )
    monkeypatch.setenv(
        "REDTEAM_MCP_MOBILE_NEXT_TOOL_OPERATIONS_JSON",
        json.dumps(operation_bindings),
    )
    monkeypatch.setenv("REDTEAM_MCP_MOBILE_NEXT_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024))

    config = load_app_config(app_path)
    config.capability.mcp_servers = ["mobile-next"]
    config.engagement.metadata["device_serial"] = _DEVICE_SERIAL
    config.engagement.metadata["device_ref"] = _DEVICE_REF
    config.engagement.metadata["package_name"] = _PACKAGE
    config.engagement.approval_policy.auto_approve = True
    settings = OrchestrationSettings.from_config(
        config.orchestration,
        checkpointing=True,
        max_steps=40,
    )
    mobile_definition = next(
        item
        for item in builtin_agent_definitions()
        if item.agent_id == MOBILE_AGENT.agent_id
    )

    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=settings,
            agents=AgentRegistry([MOBILE_AGENT], definitions=[mobile_definition]),
        )
        final = await run_engagement(engagement, deps=deps)

        assert isinstance(deps.event_bus, InMemoryEventBus)
        diagnostics = [
            event.model_dump(mode="json")
            for event in deps.event_bus.history()
            if event.event_type
            in {
                CANDIDATE_REJECTED,
                AGENT_NO_EXECUTABLE_CANDIDATES,
                EventType.POLICY_EVALUATED,
                EventType.TOOL_EXECUTION_FAILED,
            }
        ]
        diagnostic_json = json.dumps(diagnostics, indent=2)
        assert final.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN, diagnostic_json
        assert final.actions_executed in {3, 4}, diagnostic_json

        evidence = await deps.evidence_store.list_for_run(final.run_id)
        assert len(evidence) == final.actions_executed
        assert all(item.metadata["success"] is True for item in evidence), diagnostic_json

        graph = AttackGraphService(deps.graph_store)
        [asset] = await graph.query(node_type=GraphNodeType.ASSET)
        attrs = attrs_of(asset.properties)
        facts = facts_of(asset.properties)
        assert attrs["mobile_app_launched"] is True
        assert attrs["mobile_logs_observed"] is True
        assert attrs["mobile_crashes_listed"] is True

        findings = await graph.list_findings(verified=True)
        runtime_findings = [
            item
            for item in findings
            if item.metadata.get("check_id") == "android_runtime_crash"
        ]
        if "crash_id" in facts:
            assert final.actions_executed == 4
            assert attrs["mobile_crash_retrieved"] is True
            assert isinstance(facts["crash_id"], str)
            assert runtime_findings
        else:
            assert final.actions_executed == 3
            assert runtime_findings == []
        if _EXPECT_CRASH:
            assert runtime_findings, diagnostic_json
        assert (await graph.integrity_report()).blocking == []
