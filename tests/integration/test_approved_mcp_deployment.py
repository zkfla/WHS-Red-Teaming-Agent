"""Real stdio + real loop/Control + loopback-only synthetic two-principal HTTP."""

import json
import threading
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from mcp import Client

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.cli._shared import load_app_config_for_run
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.capability.servers import web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.core.config import AppConfig, CapabilityConfig, load_engagement_config
from redteam.core.enums import RiskLevel
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.integration import _engine_llm_harness as h
from tests.unit.test_mcp_deployment import engagement, source


class SessionLLM:
    """Native scripted model: inventory then two-principal compare, then hold.

    Speaks the genuine product contract (complete_native with
    NativeCompletion/NativeToolCall addressed to the exposed tool names).
    complete_structured answers only the genuine verifier meaning route;
    any legacy strategy route raises instead of returning a soft answer.
    """

    def __init__(self, seed="127.0.0.1"):
        self.calls: list[dict[str, object]] = []
        self.turns = 0
        self.node_id = "host:" + seed

    @staticmethod
    def _route_key(response_schema: dict[str, object]) -> str:
        props = response_schema.get("properties", {})
        if not isinstance(props, dict):
            return "unknown"
        if "claim_supported" in props:
            return "analysis_verify"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        return "unknown"

    async def complete(self, *, messages, system=None, **_: object) -> str:
        return ""

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        route = self._route_key(response_schema)
        self.calls.append({"route": route})
        if route == "analysis_verify":
            return h.meaning("inconclusive", False, "Only scoped observations; not proof of IDOR")
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete_native(self, messages, tools, system=None, **_: object):
        available = [str(getattr(spec, "name", "")) for spec in tools]
        inventory_matches = [name for name in available if "session_list" in name]
        compare_matches = [name for name in available if "http_compare_sessions" in name]
        assert len(inventory_matches) == 1, (
            "native model needs exactly one registered inventory tool"
        )
        assert len(compare_matches) == 1, (
            "native model needs exactly one registered compare tool"
        )
        inventory = inventory_matches[0]
        compare = compare_matches[0]
        # Preserve the deployed scope/session contract on the real context
        # snapshot instead of a retired strategy payload.
        snapshot = json.loads(messages[-1]["content"])
        constraints = snapshot.get("engagement_constraints", {})
        profile = constraints.get("mcp_targets", {}).get("bank-test", {})
        if profile:
            assert profile.get("allowed_paths") == ["/", "/accounts/123"]
        self.turns += 1
        if self.turns == 1:
            self.calls.append({"turn": 1, "tool": inventory})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-inventory-1",
                        name=inventory,
                        arguments={},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            self.calls.append({"turn": 2, "tool": compare})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-compare-1",
                        name=compare,
                        arguments={
                            "target_ref": "bank-test",
                            "path": "/accounts/123",
                            "first_session_ref": "alice",
                            "second_session_ref": "bob",
                        },
                    ),
                ),
                text="",
            )
        self.calls.append({"turn": self.turns, "tool": None})
        return NativeCompletion(
            tool_calls=(),
            text="Synthetic observation check finished",
        )


def install(tmp_path, monkeypatch, origin, *, require_approval=False, port_pinned=False):
    spec = DeploymentSpec.model_validate(source(origin))
    config = engagement()
    if port_pinned:
        config.scope.allowed_hosts = [urlsplit(origin).netloc]
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


@pytest.mark.parametrize("port_pinned", [False, True])
async def test_agent_uses_inventory_and_two_users_through_real_mcp(
    tmp_path, monkeypatch, bank, port_pinned
):
    origin, requests = bank
    config, path = install(tmp_path, monkeypatch, origin, port_pinned=port_pinned)
    cli_config = load_app_config_for_run(str(path), require_llm=False)
    assert cli_config.engagement == config.engagement
    monkeypatch.setattr("redteam.app.bootstrap.save_terminal_report", lambda report: None)
    llm = SessionLLM(config.engagement.scope.allowed_hosts[0])
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
        inventory_records = [r for r in records if r.source_id == str(inventory.tool_id)]
        assert inventory_records, "inventory discovery must be recorded"
        discovered = inventory_records[0].metadata["tool_result"]["output"]["sessions"]
        assert [entry["session_ref"] for entry in discovered] == ["alice", "bob"]
        compare_tool = deps.tool_catalog.get("web.http_compare_sessions")
        assert compare_tool is not None
        comparison = next(r for r in records if r.source_id == str(compare_tool.tool_id))
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
