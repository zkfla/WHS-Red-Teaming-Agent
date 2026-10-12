"""Real stdio + real loop/Control + loopback-only synthetic two-principal HTTP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from mcp import Client

from redteam.app.cli._shared import load_app_config_for_run
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.capability.servers import web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.enums import RiskLevel
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.integration import _engine_llm_harness as h
from tests.unit.test_mcp_deployment import engagement, source


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.delenv("REDTEAM_RUNTIME_BACKEND", raising=False)


class SessionLLM:
    """Native scripted model: discover, inventory, two-principal compare, hold.

    Speaks the genuine product contract (complete_native with
    NativeCompletion/NativeToolCall addressed to the exposed tool names).
    It follows the lazy card-first contract: tool.list_cards, then
    tool.get_detail for each needed schema, and only then the real MCP
    calls. The exactly-one inventory/compare assertions stay, enforced at
    card discovery and again when the schemas are advertised.
    complete_structured answers only the genuine verifier meaning route;
    any legacy strategy route raises instead of returning a soft answer.
    """

    def __init__(self, seed="127.0.0.1"):
        self.calls: list[dict[str, object]] = []
        self.turns = 0
        self.node_id = "host:" + seed
        self.listed = False
        self.inventory_tool_id: object = None
        self.compare_tool_id: object = None
        self.inventory_detailed = False
        self.compare_detailed = False

    def _harvest_cards(self, messages):
        """Collect (tool_name -> tool_ids) from card list results seen so far."""
        found: dict[str, set[str]] = {}
        for message in messages or []:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            try:
                body = json.loads(message.get("content") or "{}")
            except ValueError:
                continue
            payload = body.get("payload") if isinstance(body, dict) else None
            cards = payload.get("cards") if isinstance(payload, dict) else None
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                name = card.get("tool_name")
                identity = card.get("tool_id")
                if isinstance(name, str) and isinstance(identity, str):
                    found.setdefault(name, set()).add(identity)
        return found

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
        from redteam.orchestration.native_binding import exposed_tool_name
        from redteam.orchestration.tool_discovery import TOOL_GET_DETAIL_ID, TOOL_LIST_CARDS_ID

        available = [str(getattr(spec, "name", "")) for spec in tools]
        list_name = exposed_tool_name(TOOL_LIST_CARDS_ID)
        detail_name = exposed_tool_name(TOOL_GET_DETAIL_ID)
        # Preserve the deployed scope/session contract on the real context
        # snapshot instead of a retired strategy payload.
        snapshot = json.loads(messages[-1]["content"])
        constraints = snapshot.get("engagement_constraints", {})
        profile = constraints.get("mcp_targets", {}).get("bank-test", {})
        if profile:
            assert profile.get("allowed_paths") == ["/", "/accounts/123"]
        self.turns += 1
        if not self.listed:
            assert list_name in available, "native model needs the card discovery helper"
            assert detail_name in available, "native model needs the detail lookup helper"
            self.listed = True
            self.calls.append({"turn": self.turns, "tool": "list"})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-list-1",
                        name=list_name,
                        arguments={"query": "session"},
                    ),
                ),
                text="",
            )
        cards = self._harvest_cards(messages)
        inventory_ids = cards.get("session_list", set())
        compare_ids = cards.get("http_compare_sessions", set())
        assert len(inventory_ids) == 1, "native model needs exactly one inventory card"
        assert len(compare_ids) == 1, "native model needs exactly one compare card"
        if self.inventory_tool_id is None:
            self.inventory_tool_id = next(iter(inventory_ids))
        if self.compare_tool_id is None:
            self.compare_tool_id = next(iter(compare_ids))
        inventory_matches = [name for name in available if "session_list" in name]
        compare_matches = [name for name in available if "http_compare_sessions" in name]
        if not self.inventory_detailed:
            self.inventory_detailed = True
            self.calls.append({"turn": self.turns, "tool": "detail-inventory"})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-detail-inventory-1",
                        name=detail_name,
                        arguments={"tool_id": self.inventory_tool_id},
                    ),
                ),
                text="",
            )
        assert len(inventory_matches) == 1, (
            "native model needs exactly one registered inventory tool"
        )
        if not any(call.get("tool") == "inventory" for call in self.calls):
            inventory = inventory_matches[0]
            self.calls.append({"turn": self.turns, "tool": "inventory"})
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
        if not self.compare_detailed:
            self.compare_detailed = True
            self.calls.append({"turn": self.turns, "tool": "detail-compare"})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-detail-compare-1",
                        name=detail_name,
                        arguments={"tool_id": self.compare_tool_id},
                    ),
                ),
                text="",
            )
        assert len(compare_matches) == 1, "native model needs exactly one registered compare tool"
        if not any(call.get("tool") == "compare" for call in self.calls):
            compare = compare_matches[0]
            self.calls.append({"turn": self.turns, "tool": "compare"})
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
        orchestration=OrchestrationConfig(),
        engagement=loaded,
        capability=CapabilityConfig(mcp_servers=list(spec.roles)),
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


async def run_central_worker(config, tmp_path, llm):
    from redteam.app.central_run import open_central_run
    from redteam.core.contracts.llm_policy import LLMRequestPolicy

    settings = OrchestrationSettings(
        specialist_profile_path=str(
            Path(__file__).resolve().parents[2] / "config/agents/native-specialists-web.yaml"
        ),
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=50),
        max_steps=20,
    )
    return open_central_run(config, directory=tmp_path / "run", settings=settings, llm=llm)


async def register_central_comparison(session):
    from redteam.orchestration.knowledge_validation import known_target_ids
    from redteam.orchestration.specialist_tasks import StrategyTasks, register_investigation_tasks
    material = await InvestigationEngine(session.deps).execution_material(session.runtime)
    targets = set(known_target_ids(material))
    assert targets
    condition = {"kind": "semantic", "description": "Observe scoped session responses"}
    subject = {
        "question_id": "compare",
        "objective": "Compare synthetic test users",
        "target_node_ids": sorted(targets),
        "completion_criteria": [condition],
    }
    await register_investigation_tasks(
        session.deps.central_state,
        proposal_id="compare",
        proposal=StrategyTasks.model_validate(
            {
                "questions": [subject],
                "tasks": [
                    {
                        **subject,
                        "task_key": "compare",
                        "role_id": "web-executor",
                        "title": "Observe session responses",
                    }
                ],
            }
        ),
        agents=session.deps.agents,
        known_targets=targets,
        known_principals=set(),
        max_tasks=1,
        evidence_store=session.deps.evidence_store,
    )


@pytest.mark.parametrize("port_pinned", [False, True])
async def test_agent_uses_inventory_and_two_users_through_real_mcp(
    tmp_path,
    monkeypatch,
    bank,
    port_pinned,
):
    origin, requests = bank
    config, path = install(tmp_path, monkeypatch, origin, port_pinned=port_pinned)
    assert load_app_config_for_run(str(path), require_llm=False).engagement == config.engagement
    llm = SessionLLM(config.engagement.scope.allowed_hosts[0])
    async with await run_central_worker(config, tmp_path, llm) as session:
        deps = session.deps
        assert len(deps.tool_catalog.all()) == 11
        await register_central_comparison(session)
        results = [
            event
            async for event in session.driver.drive_ready_tasks(
                session.runtime,
                max_instances=2,
            )
        ]
        final = results[0].runtime
        assert final.actions_executed == 2, (final.termination_reason, final.planning_feedback)
        assert requests == ["/accounts/123", "/accounts/123"]
        records = await deps.evidence_store.list_for_run(final.run_id)
        inventory = deps.tool_catalog.get("web.session_list")
        inventory_record = next(
            record for record in records if record.source_id == str(inventory.tool_id)
        )
        discovered = inventory_record.metadata["tool_result"]["output"]["sessions"]
        assert [row["session_ref"] for row in discovered] == ["alice", "bob"]
        comparison = next(
            record
            for record in records
            if record.source_id == str(deps.tool_catalog.get("web.http_compare_sessions").tool_id)
        )
        output = comparison.metadata["tool_result"]["output"]
        assert output["first"]["status_code"] == 200 and output["second"]["status_code"] == 403
        assert output["verdict"] == "observation_only"
        context = await InvestigationEngine(deps).context(final, None)
        exposed = json.dumps(
            [llm.calls, context, [record.model_dump(mode="json") for record in records]]
        )
        assert "synthetic-a" not in exposed and "synthetic-b" not in exposed
        assert "BODY_MUST_NOT_REACH_AGENT" not in exposed
        assert final.termination_reason != TerminationReason.GOAL_ACHIEVED


async def test_real_control_still_requires_approval_before_mcp_call(tmp_path, monkeypatch, bank):
    origin, requests = bank
    config, _ = install(tmp_path, monkeypatch, origin, require_approval=True)
    async with await run_central_worker(config, tmp_path, SessionLLM()) as session:
        await register_central_comparison(session)
        results = [
            event
            async for event in session.driver.drive_ready_tasks(
                session.runtime,
                max_instances=2,
            )
        ]
        final = results[0].runtime
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
