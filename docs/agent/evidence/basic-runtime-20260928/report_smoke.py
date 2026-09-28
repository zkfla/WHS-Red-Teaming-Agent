"""Report verification: committed dev, real stdio MCP and local mock HTTP, fake LLM."""
import asyncio
import importlib.util
import json
import os
import sys
import threading
from collections import Counter
from contextlib import AsyncExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventType
from redteam.orchestration.settings import OrchestrationSettings

ROOT = Path("/review")
spec = importlib.util.spec_from_file_location("synthetic_bank", ROOT / "deploy/docker/finance-mock/server.py")
bank = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bank)
server = ThreadingHTTPServer(("127.0.0.1", 0), bank.Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

def configure(directory):
    policy = json.loads((ROOT / "deploy/mcp/finance/policy.json").read_text())
    policy["targets"]["bank-lab"]["origin"] = f"http://127.0.0.1:{server.server_port}"
    policy["allowed_networks"] = ["127.0.0.0/8"]
    policy_path = directory / "policy.json"
    policy_path.write_text(json.dumps(policy))
    bindings = [
        ("WEB_HTTP", "web_api", "web-http", "high",
         {"http_request": ["http_request"], "http_security_posture": ["response_analysis"]}),
        ("RECON_PASSIVE", "recon", "recon-passive", "medium",
         {"http_probe": ["service_discovery"], "html_inventory": ["endpoint_discovery"]}),
    ]
    for key, kind, role, risk, operations in bindings:
        settings = {
            "KIND": kind, "TRANSPORT": "stdio", "COMMAND": sys.executable,
            "ARGS_JSON": json.dumps(["-m", "redteam.capability.servers", role, "--policy", str(policy_path)]),
            "RISK_LEVEL": risk, "MAX_CONCURRENCY": "1", "MAX_OUTPUT_BYTES": "262144",
            "ALLOWED_TOOLS_JSON": json.dumps(list(operations)),
            "TOOL_OPERATIONS_JSON": json.dumps(operations),
        }
        for suffix, value in settings.items():
            os.environ[f"REDTEAM_MCP_{key}_{suffix}"] = value

async def exercise(directory, require_approval):
    config = load_app_config(str(ROOT / "deploy/mcp/finance/app.yaml"))
    config.engagement.scope.allowed_hosts = ["127.0.0.1"]
    config.engagement.metadata = {"target_refs": {"127.0.0.1": "bank-lab"}}
    config.engagement.rate_limit_policy.max_actions_per_minute = 60
    config.capability.mcp_servers = ["web-http", "recon-passive"]
    assert config.llm.provider == "fake"
    if not require_approval:
        config.engagement.approval_policy.require_approval_for = []
        config.engagement.approval_policy.auto_approve = True
    events = []
    async def collect(event):
        events.append(event)
    base = directory / ("approval" if require_approval else "loop")
    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config, mcp_stack=stack,
            evidence_dir=base / "evidence", audit_dir=base / "audit", approval_dir=base / "approvals",
            orchestration_settings=OrchestrationSettings(checkpointing=False, max_steps=2 if require_approval else 6),
        )
        await deps.event_bus.subscribe("*", collect)
        final = await run_engagement(engagement, deps=deps)
        graph = AttackGraphService(deps.graph_store)
        nodes = await graph.list_nodes()
        findings = await graph.list_findings()
        evidence = await deps.evidence_store.list_for_run(final.run_id)
        started = [e.payload["tool_id"] for e in events if e.event_type == EventType.TOOL_EXECUTION_STARTED]
        failures = [e for e in events if e.event_type == EventType.TOOL_EXECUTION_FAILED]
        disallowed = [n for n in nodes if attrs_of(n.properties).get("path_allowlisted") is False]
        evidence_ids = {str(e.evidence_id) for e in evidence}
        refs = {str(ref) for n in nodes for ref in facts_of(n.properties).get("evidence_ids", [])}
        summary = {
            "require_approval": require_approval, "llm_provider": config.llm.provider,
            "transport": "stdio", "termination": final.termination_reason,
            "actions": final.actions_executed, "actions_by_agent": final.actions_by_agent,
            "tools": started, "tool_failures": len(failures),
            "node_types": dict(Counter(n.type.value for n in nodes)),
            "nodes": [{"id": str(n.id), "type": n.type.value} for n in nodes],
            "evidence_count": len(evidence), "graph_evidence_references": len(refs),
            "graph_evidence_references_resolve": refs.issubset(evidence_ids),
            "findings": len(findings),
            "disallowed_paths": [str(n.id) for n in disallowed],
            "disallowed_paths_not_requested": all("web.http_request" not in facts_of(n.properties).get("attempted_operations", []) for n in disallowed),
            "trace": [{"event": e.event_type, "tool_id": e.payload.get("tool_id"), "node_id": e.payload.get("node_id")}
                      for e in events if e.event_type in {EventType.TOOL_EXECUTION_STARTED, EventType.GRAPH_UPDATED, EventType.APPROVAL_REQUIRED, "ReportProjected"}],
        }
        print("RESULT_JSON=" + json.dumps(summary, ensure_ascii=False), flush=True)
        assert "recon.http_probe" in started and not failures
        assert refs and refs.issubset(evidence_ids)
        if require_approval:
            assert final.termination_reason == "approval_pending"
            assert "web.http_request" not in started
        else:
            assert {"web.http_request", "recon.html_inventory", "web.http_security_posture"} <= set(started)
            assert disallowed and summary["disallowed_paths_not_requested"]
            assert final.actions_by_agent.get("web-api-agent", 0) >= 1

async def main():
    with TemporaryDirectory() as name:
        directory = Path(name)
        configure(directory)
        await exercise(directory, True)
        await exercise(directory, False)

try:
    asyncio.run(main())
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)

