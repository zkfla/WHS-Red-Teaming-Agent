"""Real wire discovery always; original backend tests opt in with isolated Python."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.vdb_manifest import VDB_TOOLS, validate_vdb_descriptor
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import AssetTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.convert import NonAllowDecisionError, build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, InvocationId, RunId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft

REPO = Path(__file__).resolve().parents[2]


def configure(monkeypatch, tmp_path, python=sys.executable):
    root = tmp_path / "db"
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "root": str(root),
                "asset_ref": "vdb-test",
                "engagement_id": "eng-test",
                "run_id": "run-test",
                "synthetic": True,
            }
        ),
        encoding="utf-8",
    )
    values = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": python,
        "ARGS_JSON": json.dumps(
            ["-m", "redteam.capability.servers.vdb_readonly", "--policy", str(policy)]
        ),
        "PROFILE": "vdb_readonly",
        "VDB_ASSET_REF": "vdb-test",
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": json.dumps(list(VDB_TOOLS)),
        "TOOL_OPERATIONS_JSON": json.dumps({n: [s.operation] for n, s in VDB_TOOLS.items()}),
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)
    return root


def call(name, **args):
    return ToolInvocation(
        invocation_id=InvocationId("inv-vdb"),
        action_id=ActionId("act-vdb"),
        run_id=RunId("run-test"),
        tool_id=ToolId(f"analysis.{name}"),
        operation=VDB_TOOLS[name].operation,
        arguments={"asset_ref": "vdb-test", **args},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"), run_id=RunId("run-test")
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_stdio_catalog_missing_status_and_fail_closed(monkeypatch, tmp_path):
    root = configure(monkeypatch, tmp_path)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        assert {
            str(t.tool_id) for t in gateway.list_tools_for_action("analysis", "static_analysis")
        } == {
            "analysis.search_by_cve",
            "analysis.search_by_purl_like",
        }
        for name in VDB_TOOLS:
            descriptor = gateway.get(ToolId(f"analysis.{name}"))
            validate_vdb_descriptor(descriptor, "vdb-test")
            for change in ("output", "required", "input", "operation", "target"):
                broken = deepcopy(descriptor)
                if change == "output":
                    broken.output_schema = {}
                elif change == "required":
                    broken.input_schema["required"].remove("asset_ref")
                elif change == "input":
                    broken.input_schema["properties"]["asset_ref"]["const"] = "other"
                elif change == "operation":
                    broken.operations = ["http_request"]
                else:
                    broken.target_types = ["host"]
                with pytest.raises(ValueError):
                    validate_vdb_descriptor(broken, "vdb-test")
        status = await gateway.invoke(call("vdb_status"))
        assert status.success and status.output["state"] == "missing"
        assert not root.exists()  # discovery/status never creates an absent root
        bad = await gateway.invoke(call("search_by_cve", cve_id="CVE-2026-%"))
        assert not bad.success and bad.error.code == "vdb_invalid_arguments"
        assert not (await gateway.invoke(call("vdb_status", sql="SELECT 1"))).success
        assert not (await gateway.invoke(call("vdb_status", run_id="other"))).success
        assert not (await gateway.invoke(call("vdb_status", asset_ref="other"))).success


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    python = os.environ.get("REDTEAM_VDB_TEST_PYTHON")
    if not python:
        pytest.skip(
            "set REDTEAM_VDB_TEST_PYTHON to isolated VDB7 interpreter; no download in tests"
        )
    root = configure(monkeypatch, tmp_path, python)
    subprocess.run(
        [python, str(REPO / "examples/build_vdb_synthetic_fixture.py"), "--root", str(root)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return root


def snapshot(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}


async def test_original_library_matches_versions_and_never_writes(seeded):
    before = snapshot(seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        status = await gateway.invoke(call("vdb_status"))
        assert status.success and status.output["state"] == "ready", status
        assert status.output["freshness"] == "fresh"
        cve = await gateway.invoke(call("search_by_cve", cve_id="CVE-2026-900001"))
        assert cve.success and len(cve.output["matches"]) == 1, cve
        assert cve.output["matches"][0]["source"] == "synthetic"
        assert not cve.output["target_vulnerability_confirmed"]
        vulnerable = await gateway.invoke(
            call("search_by_purl_like", purl="pkg:npm/redteam-synthetic-demo@1.5.0", limit=2)
        )
        assert vulnerable.success and len(vulnerable.output["matches"]) == 2, vulnerable
        assert vulnerable.output["truncated"]
        patched = await gateway.invoke(
            call("search_by_purl_like", purl="pkg:npm/redteam-synthetic-demo@2.0.0")
        )
        assert patched.success and patched.output["matches"] == [], patched
        assert patched.output["interpretation"] == "no_known_match_in_loaded_snapshot"
        for purl in (
            "pkg:npm/redteam-synthetic-demo",
            "pkg:npm/redteam-synthetic-demo@*",
            "https://example.invalid",
            "pkg:npm/redteam-synthetic-demo@1.5.0#path",
        ):
            assert not (await gateway.invoke(call("search_by_purl_like", purl=purl))).success
        assert str(seeded) not in json.dumps(cve.output)
    assert snapshot(seeded) == before


@pytest.mark.parametrize(
    "case,state,freshness",
    [
        ("empty", "empty", "unknown"),
        ("partial", "coverage_gap", "fresh"),
        ("stale", "ready", "stale"),
        ("missing_meta", "unknown_metadata", "unknown"),
        ("invalid_meta", "invalid", "unknown"),
        ("missing_db", "missing", "unknown"),
        ("future_meta", "ready", "unknown"),
    ],
)
async def test_snapshot_state_is_not_silently_clean(seeded, case, state, freshness):
    meta = seeded / "vdb.meta"
    if case == "empty":
        import sqlite3

        with sqlite3.connect(seeded / "data.index.vdb7") as conn:
            conn.execute("DELETE FROM cve_index")
    elif case == "missing_meta":
        meta.unlink()
    elif case == "missing_db":
        (seeded / "data.vdb7").unlink()
    elif case == "invalid_meta":
        meta.write_text("not JSON", encoding="utf-8")
    else:
        metadata = json.loads(meta.read_text())
        if case == "partial":
            metadata["completeness"] = "partial"
        else:
            days = -30 if case == "stale" else 30
            metadata["created_utc"] = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).isoformat()
        meta.write_text(json.dumps(metadata), encoding="utf-8")
    before = snapshot(seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        status = await gateway.invoke(call("vdb_status"))
        assert status.success and status.output["state"] == state, status
        assert status.output["freshness"] == freshness
        lookup = await gateway.invoke(call("search_by_cve", cve_id="CVE-2026-900001"))
        assert lookup.success == (state == "ready"), lookup
        if lookup.success:
            assert lookup.output["database"]["freshness"] == freshness
        else:
            assert str(seeded) not in lookup.error.message
    assert snapshot(seeded) == before


async def test_candidate_control_and_actual_vdb_lookup(
    seeded,
    *,
    tool_name="search_by_cve",
    arguments=None,
):
    target = AssetTarget(asset_id=AssetId("vdb-test"))
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        draft = CandidateDraft(
            agent_id=AgentId("knowledge-agent"),
            target_node_ids=[node_id_for_target(target)],
            plan_step=PlanStep(
                step_id="lookup",
                capability="analysis",
                operation="static_analysis",
                tool_id=ToolId(f"analysis.{tool_name}"),
                target=target,
                arguments=arguments or {"cve_id": "CVE-2026-900001"},
                reason="investigate knowledge only",
                assessment_kind="observation",
            ),
        )
        compiled, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections([draft])
        assert len(compiled) == 1 and not rejected
        step = compiled[0].plan_step
        assert step.arguments["asset_ref"] == "vdb-test"
        engagement = Engagement(
            engagement_id=EngagementId("eng-test"),
            name="synthetic VDB",
            scope=EngagementScope(allowed_assets=["vdb-test"]),
            execution_budget=ExecutionBudget(max_actions=5),
            rate_limit_policy=RateLimitPolicy(max_actions_per_minute=5),
            approval_policy=ApprovalPolicy(),
            created_at=datetime.now(timezone.utc),
        )
        plane = DefaultControlPlane(
            {engagement.engagement_id: engagement}, risk_resolver=RegistryRiskResolver(gateway)
        )
        proposal = ActionProposal(
            action_id=ActionId("act-vdb"),
            run_id=RunId("run-test"),
            engagement_id=engagement.engagement_id,
            agent_id=draft.agent_id,
            capability=step.capability,
            operation=step.operation,
            target=step.resolved_target(),
            arguments=step.arguments,
            reason=step.reason,
            created_at=datetime.now(timezone.utc),
        )
        decision = await plane.evaluate(
            proposal, ControlContext(engagement_id=engagement.engagement_id)
        )
        assert decision.verdict == PolicyVerdict.ALLOW
        result = await gateway.invoke(
            build_tool_invocation(proposal, decision, tool_id=step.tool_id)
        )
        assert result.success and (result.output.get("matches") or result.output.get("items")), (
            result
        )
        proposal.arguments["asset_ref"] = "different-db"
        refused = await plane.evaluate(
            proposal, ControlContext(engagement_id=engagement.engagement_id)
        )
        assert refused.verdict == PolicyVerdict.DENY
        with pytest.raises(NonAllowDecisionError):
            build_tool_invocation(proposal, refused, tool_id=step.tool_id)


async def test_low_operator_limit_has_valid_discovery_default(seeded):
    policy = seeded.parent / "policy.json"
    values = json.loads(policy.read_text())
    values["max_results"] = 1
    policy.write_text(json.dumps(values))
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        descriptor = gateway.get(ToolId("analysis.search_by_purl_like"))
        assert descriptor.input_schema["properties"]["limit"]["default"] == 1
        result = await gateway.invoke(
            call("search_by_purl_like", purl="pkg:npm/redteam-synthetic-demo@1.5.0")
        )
        assert result.success and len(result.output["matches"]) == 1
        assert result.output["truncated"]


async def test_unfinished_wal_snapshot_rejected_without_side_effects(seeded):
    (seeded / "data.index.vdb7-wal").write_bytes(b"unfinished operator build")
    before = snapshot(seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        result = await gateway.invoke(call("vdb_status"))
        assert result.success and result.output["state"] == "invalid"
        assert not (await gateway.invoke(call("search_by_cve", cve_id="CVE-2026-900001"))).success
    assert snapshot(seeded) == before


async def test_backend_connections_are_strict_readonly(seeded, monkeypatch):
    # Use isolated interpreter so the main project's environment stays unchanged.
    python = os.environ["REDTEAM_VDB_TEST_PYTHON"]
    code = """
from redteam.capability.servers.vdb_readonly import VDBPolicy,VDBReader
import sys
p=VDBPolicy(root=sys.argv[1],asset_ref="test",engagement_id="eng",run_id="run")
with VDBReader(p)._connections() as connections:
    for c in connections:
        assert c.readonly("main")
        try:
            c.execute("CREATE TABLE must_not_exist(x)")
        except Exception:
            pass
        else:
            raise AssertionError("write unexpectedly accepted")
"""
    before = snapshot(seeded)
    subprocess.run([python, "-c", code, str(seeded)], check=True, capture_output=True, timeout=20)
    assert snapshot(seeded) == before


async def test_metadata_counts_disagree_with_snapshot_is_invalid(seeded):
    meta = seeded / "vdb.meta"
    values = json.loads(meta.read_text())
    values["cve_index_count"] = 1000
    meta.write_text(json.dumps(values))
    before = snapshot(seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        result = await gateway.invoke(call("vdb_status"))
        assert result.success and result.output["state"] == "invalid"
        assert not (await gateway.invoke(call("search_by_cve", cve_id="CVE-2026-900001"))).success
    assert snapshot(seeded) == before
