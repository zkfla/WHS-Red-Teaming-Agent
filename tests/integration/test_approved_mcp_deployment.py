"""Real stdio + real loop/Control + loopback-only synthetic two-principal HTTP."""

import json
import threading
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import Client

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.cli._shared import load_app_config_for_run
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.capability.servers import web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.core.config import AppConfig, CapabilityConfig, load_engagement_config
from redteam.core.enums import RiskLevel
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.integration import _engine_llm_harness as h
from tests.unit.test_mcp_deployment import engagement, source


class SessionLLM(h.ScriptedLLM):
    """Explicit fixture chooses from actual deployment context; never a production model."""

    def __init__(self):
        super().__init__()
        self.turns = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        route = self._route_key(response_schema)
        self.calls.append({"messages": messages, "system": system, "route": route})
        payload = json.loads(messages[0]["content"])
        if route == "path_draft":
            path = h.draft_path("host:127.0.0.1")
            step = path["steps"][0]
            step.update(
                agent_id="web-api-agent",
                principal_refs=["alice", "bob"],
                allowed_operations=["web.response_analysis"],
            )
            return path
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("inconclusive", False, "Only scoped observations; not proof of IDOR")
        if route == "next_action":
            self.turns += 1
            profile = payload["engagement_constraints"]["mcp_targets"]["bank-test"]
            assert profile["allowed_paths"] == ["/", "/accounts/123"]
            assert [s["session_ref"] for s in profile["sessions"]] == ["alice", "bob"]
            if self.turns > 2:
                return {
                    "mode": "hold",
                    "reason": "Synthetic observation check finished; no vulnerability claim",
                }
            inventory = self.turns == 1
            return {
                "mode": "actions",
                "reason": "Synthetic test only",
                "actions": [
                    {
                        "agent_id": "web-api-agent",
                        "path_step_id": "step-1",
                        "target_node_id": "host:127.0.0.1",
                        "principal_ref": "alice",
                        "capability": "web",
                        "operation": "response_analysis",
                        "tool_id": "web.session_list" if inventory else "web.http_compare_sessions",
                        "arguments": {}
                        if inventory
                        else {
                            "target_ref": "bank-test",
                            "path": "/accounts/123",
                            "first_session_ref": "alice",
                            "second_session_ref": "bob",
                        },
                        "reason": "Read registered references or compare synthetic test users",
                    }
                ],
            }
        raise AssertionError(route)


def install(tmp_path, monkeypatch, origin, *, require_approval=False):
    spec = DeploymentSpec.model_validate(source(origin))
    config = engagement()
    config.approval_policy.require_approval_for = []
    # Explicit fixture policy only; production generator preserves the input policy.
    config.approval_policy.risk_threshold = (
        RiskLevel.HIGH if require_approval else RiskLevel.CRITICAL
    )
    path = provision(spec, config, tmp_path / "approved")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]
    monkeypatch.setenv("TEST_ALICE", "synthetic-a")
    monkeypatch.setenv("TEST_BOB", "synthetic-b")
    for key, value in connection_environment(
        spec, binding["policy_path"], binding["policy_sha256"]
    ).items():
        monkeypatch.setenv(key, value)
    return AppConfig(
        engagement=loaded, capability=CapabilityConfig(mcp_servers=list(spec.roles))
    ), path


@pytest.fixture
def bank():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            assert self.path == "/accounts/123"
            auth = self.headers.get("Authorization")
            assert auth in {"Bearer synthetic-a", "Bearer synthetic-b"}
            self.send_response(200 if auth == "Bearer synthetic-a" else 403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"synthetic_private_account":"BODY_MUST_NOT_REACH_AGENT"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_agent_uses_inventory_and_two_users_through_real_mcp(tmp_path, monkeypatch, bank):
    origin, requests = bank
    config, path = install(tmp_path, monkeypatch, origin)
    cli_config = load_app_config_for_run(str(path), require_llm=False)
    assert cli_config.engagement == config.engagement
    monkeypatch.setattr("redteam.app.bootstrap.save_terminal_report", lambda report: None)
    llm = SessionLLM()
    async with AsyncExitStack() as stack:
        eng, deps = await build_application(
            config,
            mcp_stack=stack,
            llm=llm,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=OrchestrationSettings(
                max_steps=20,
                checkpoint_backend="memory",
                enforce_verification_reserve=False,
            ),
        )
        assert len(deps.tool_catalog.all()) == 11
        inventory = deps.tool_catalog.get("web.session_list")
        assert inventory.operations == ["response_analysis"]
        final = await run_engagement(eng, deps, objective="Compare only synthetic test users")
        assert final.actions_executed == 2, (final.termination_reason, final.planning_feedback)
        assert requests == ["/accounts/123", "/accounts/123"]
        records = await deps.evidence_store.list_for_run(final.run_id)
        comparison = next(r for r in records if r.source_id == "web.http_compare_sessions")
        output = comparison.metadata["tool_result"]["output"]
        assert output["first"]["status_code"] == 200 and output["second"]["status_code"] == 403
        assert output["verdict"] == "observation_only"
        context = await InvestigationEngine(deps).context(final, None)
        assert context["engagement_constraints"]["mcp_targets"]["bank-test"]["origin"] == origin
        exposed = json.dumps([llm.calls, context, [r.model_dump(mode="json") for r in records]])
        assert "synthetic-a" not in exposed and "synthetic-b" not in exposed
        assert "BODY_MUST_NOT_REACH_AGENT" not in exposed
        assert final.termination_reason != TerminationReason.GOAL_ACHIEVED


async def test_real_control_still_requires_approval_before_mcp_call(tmp_path, monkeypatch, bank):
    origin, requests = bank
    config, _ = install(tmp_path, monkeypatch, origin, require_approval=True)
    monkeypatch.setattr("redteam.app.bootstrap.save_terminal_report", lambda report: None)
    async with AsyncExitStack() as stack:
        eng, deps = await build_application(
            config,
            mcp_stack=stack,
            llm=SessionLLM(),
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=OrchestrationSettings(max_steps=12, checkpoint_backend="memory"),
        )
        final = await run_engagement(eng, deps)
        assert final.pending_approval_id and final.actions_executed == 0
        assert requests == []


async def test_second_credential_failure_does_not_send_the_first_request(monkeypatch):
    spec = DeploymentSpec.model_validate(source())
    policy = spec.server_policy()
    monkeypatch.setenv("TEST_ALICE", "synthetic-a")
    monkeypatch.delenv("TEST_BOB", raising=False)
    called = []

    async def never_request(*args, **kwargs):
        called.append(kwargs)
        raise AssertionError("No network request should be sent")

    monkeypatch.setattr(web_http, "request", never_request)
    async with Client(web_http.create_server(policy)) as client:
        result = await client.call_tool(
            "http_compare_sessions",
            {
                "target_ref": "bank-test",
                "path": "/accounts/123",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            },
        )
    assert result.is_error and not called


async def test_second_session_path_failure_is_preflighted(monkeypatch):
    value = DeploymentSpec.model_validate(source()).server_policy().model_dump()
    value["sessions"]["bob"]["allowed_paths"] = ["/"]
    policy = ServerPolicy.model_validate(value)
    monkeypatch.setenv("TEST_ALICE", "synthetic-a")
    monkeypatch.setenv("TEST_BOB", "synthetic-b")
    called = []

    async def never_request(*args, **kwargs):
        called.append(kwargs)
        raise AssertionError("No network request should be sent")

    monkeypatch.setattr(web_http, "request", never_request)
    async with Client(web_http.create_server(policy)) as client:
        result = await client.call_tool(
            "http_compare_sessions",
            {
                "target_ref": "bank-test",
                "path": "/accounts/123",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            },
        )
    assert result.is_error and not called
