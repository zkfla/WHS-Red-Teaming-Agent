"""Opt-in full Analysis Agent engagement against the real apktool MCP image."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.mobile_analysis_manifest import MOBILE_ANALYSIS_TOOLS
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventType
from redteam.core.enums import GraphNodeType
from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES, CANDIDATE_REJECTED
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason

_APK_ENV = "REDTEAM_TEST_APKTOOL_E2E_APK"
_APK_PATH = os.environ.get(_APK_ENV)


@pytest.mark.skipif(not _APK_PATH, reason=f"set {_APK_ENV} to an authorized APK")
async def test_real_apktool_mcp_completes_the_analysis_agent_engagement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apk_path = Path(_APK_PATH or "").resolve()
    assert apk_path.is_file()

    repository_root = Path(__file__).resolve().parents[2]
    compose_path = repository_root / "deploy" / "compose" / "docker-compose.yml"
    app_path = repository_root / "deploy" / "mcp" / "mobile-analysis" / "app.yaml"
    specs = MOBILE_ANALYSIS_TOOLS["apktool"]
    tool_names = [spec.tool_id.partition(".")[2] for spec in specs]
    operation_bindings = {
        spec.tool_id.partition(".")[2]: [spec.operation] for spec in specs
    }

    monkeypatch.setenv("APKTOOL_MCP_WORKDIR", str(apk_path.parent))
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_KIND", "analysis")
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_TRANSPORT", "stdio")
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_COMMAND", "docker")
    monkeypatch.setenv(
        "REDTEAM_MCP_APKTOOL_ARGS_JSON",
        json.dumps(
            [
                "compose",
                "-f",
                str(compose_path),
                "run",
                "--rm",
                "-i",
                "apktool-mcp",
            ]
        ),
    )
    passthrough = ["APKTOOL_MCP_WORKDIR"]
    if "ProgramFiles" in os.environ:
        passthrough.insert(0, "ProgramFiles")
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_PASSTHROUGH_ENV", ",".join(passthrough))
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_RISK_LEVEL", "low")
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_ALLOWED_TOOLS_JSON", json.dumps(tool_names))
    monkeypatch.setenv(
        "REDTEAM_MCP_APKTOOL_TOOL_OPERATIONS_JSON",
        json.dumps(operation_bindings),
    )
    monkeypatch.setenv("REDTEAM_MCP_APKTOOL_MAX_OUTPUT_BYTES", str(16 * 1024 * 1024))

    config = load_app_config(app_path)
    config.capability.mcp_servers = ["apktool"]
    config.engagement.metadata["asset_ref"] = f"/work/{apk_path.name}"
    settings = OrchestrationSettings.from_config(
        config.orchestration,
        checkpointing=True,
        max_consecutive_failures=1,
    )

    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=settings,
        )
        final = await run_engagement(engagement, deps=deps)

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
        assert final.actions_executed == 4, diagnostic_json
        evidence = await deps.evidence_store.list_for_run(final.run_id)
        assert len(evidence) == 4
        assert all(item.metadata["success"] is True for item in evidence)

        graph = AttackGraphService(deps.graph_store)
        findings = await graph.list_findings(verified=True)
        assert {item.metadata["check_id"] for item in findings} >= {
            "android_manifest_debuggable",
            "android_manifest_exported_components",
            "android_manifest_dangerous_permissions",
        }
        assets = await graph.query(node_type=GraphNodeType.ASSET)
        project_dir = facts_of(assets[0].properties)["project_dir"]
        assert project_dir.startswith("/work/")
        assert (await graph.integrity_report()).blocking == []
