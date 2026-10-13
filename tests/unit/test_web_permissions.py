"""Explicit web permissions reach provider execution; synthetic HTTP only."""

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from redteam.app.cli.conversation import ConversationAssistant
from redteam.app.cli.input_router import InputRouter
from redteam.app.cli.tool_context import configured_tool_context, live_tool_context
from redteam.app.mcp_deployment import DeploymentBinding
from redteam.app.web.engagement import EngagementError
from redteam.app.web_policy import WebPolicySelection, prepare_web_deployment
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ToolInputError, request
from redteam.core.environment import environment_context
from tests.unit.test_cli_conversation import _ScriptedDecisionLLM
from tests.unit.test_web_policy import DRAFT, save


async def prepared(store):
    with environment_context({}):
        environment = await prepare_web_deployment(store.path)
    binding = DeploymentBinding.model_validate(store.load().metadata["mcp_deployment"])
    policy = ServerPolicy.load(binding.policy_path, expected_sha256=binding.policy_sha256)
    return policy, environment


def pin_dns(monkeypatch):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)


async def test_full_generated_policy_executes_form_query_and_custom_header(tmp_path, monkeypatch):
    store = save(tmp_path / "lab.yaml", web_permissions="full", web_response_body_chars=4096,
                 kali_network_mode="closed")
    policy, environment = await prepared(store)
    target_ref, = policy.targets
    pin_dns(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        assert req.method == "POST" and req.url.params["search"] == "probe"
        assert req.headers["x-test"] == "marker"
        assert req.headers["content-type"] == "application/x-www-form-urlencoded"
        assert req.content == b"message=probe"
        return httpx.Response(200, json={"message": "probe", "token": "must-hide"})
    result = await request(policy, target_ref, "/arbitrary", method="POST",
                           query={"search": "probe"}, request_headers={"X-Test": "marker"},
                           form_body={"message": "probe"}, transport=httpx.MockTransport(handler))
    summary = result.summary(body_max_chars=policy.targets[target_ref].response_body_max_chars)
    assert "probe" in summary["body_preview"] and "must-hide" not in str(summary)
    assert summary["body_preview_is_untrusted_data"] is True
    assert len(calls) == 1 and environment["REDTEAM_GATED_NETWORK_POLICY_PATH"] == ""
    assert store.load().metadata["mcp_targets"][target_ref]["allow_all_query_keys"] is True
    assert store.status()["draft"]["web_permissions"] == "full"


@pytest.mark.parametrize("arguments", [
    {"request_headers": {"Host": "outside.test"}},
    {"request_headers": {"Authorization": "Bearer injected"}},
    {"request_headers": {"Cookie": "injected"}},
    {"query": {"bad key": "value"}},
    {"query": {"q": "value\r\n"}},
    {"path": "//outside.test"},
])
async def test_full_keeps_authority_and_input_checks(arguments):
    selection = WebPolicySelection(mode="site", origins=["http://lab.test"], permissions="full")
    spec = selection.spec(["127.0.0.1/32"])
    target, = spec.policy.targets
    args = {"path": "/", **arguments}
    with pytest.raises(ToolInputError):
        await request(spec.policy, target, **args)


async def test_custom_permissions_are_enforced_on_all_selected_paths(tmp_path, monkeypatch):
    store = save(tmp_path / "lab.yaml", web_policy_mode="paths", web_paths=["/", "/api"],
                 web_permissions="custom", web_methods=["GET", "POST"],
                 web_query_keys=["id"], web_request_headers=["X-Probe"], web_request_body=False)
    policy, _ = await prepared(store)
    target, = policy.targets
    pin_dns(monkeypatch)
    response = await request(policy, target, "/api", query={"id": "1"},
                             request_headers={"x-probe": "yes"},
                             transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    assert response.status == 200
    for kwargs in ({"method": "DELETE"}, {"query": {"other": "1"}},
                   {"method": "POST", "json_body": {"id": "1"}},
                   {"method": "POST", "form_body": {"id": "1"}}):
        with pytest.raises(ToolInputError):
            await request(policy, target, "/api", **kwargs)
    with pytest.raises(ToolInputError):
        await request(policy, target, "/other")


async def test_permission_edit_creates_new_artifacts_and_retains_old_policy(tmp_path):
    store = save(tmp_path / "lab.yaml")
    old_policy, _ = await prepared(store)
    old_path = Path(store.load().metadata["mcp_deployment"]["policy_path"])
    old_bytes = old_path.read_bytes()
    changed = {**DRAFT, "web_permissions": "full"}
    assert store.preview(changed)["scope_expanded"]
    with pytest.raises(EngagementError, match="confirmation_required"):
        store.save(changed, confirm_scope=True, confirm_expansion=False)
    store.save(changed, confirm_scope=True, confirm_expansion=True)
    new_policy, _ = await prepared(store)
    assert old_path.read_bytes() == old_bytes
    assert new_policy != old_policy
    assert next(iter(old_policy.targets.values())).allowed_methods == ["GET", "HEAD", "OPTIONS"]
    assert not store.preview(DRAFT)["scope_expanded"]


async def test_session_references_are_bound_without_reading_credentials(tmp_path):
    session = {"session_ref": "alice", "principal_ref": "alice",
               "origin": DRAFT["allowed_hosts"][0],
               "paths": ["/account"], "bearer_env": "LAB_ALICE_TOKEN"}
    store = save(tmp_path / "lab.yaml", web_sessions=[session])
    policy, _ = await prepared(store)
    assert policy.sessions["alice"].allowed_paths == ["/account"]
    assert store.status()["draft"]["web_sessions"][0]["bearer_env"] == "LAB_ALICE_TOKEN"
    for change in ({"token": "inline-secret"}, {"bearer_env": "PATH"},
                   {"origin": "https://other.test"}, {"paths": ["//other.test"]}):
        with pytest.raises(EngagementError):
            store.preview({**DRAFT, "web_sessions": [{**session, **change}]})


def test_response_preview_is_opt_in_redacted_bounded_and_binary_omitted():
    response = CapturedResponse(200, {"content-type": "application/json"},
                                b'{"password":"never-show", "message":"marker"}')
    assert response.summary()["body_omitted"] is True
    preview = response.summary(body_max_chars=8)
    assert "never-show" not in str(preview)
    assert len(preview["body_preview"]) == 8 and preview["body_preview_truncated"]
    binary = CapturedResponse(200, {"content-type": "image/png"}, b"private binary")
    assert binary.summary(body_max_chars=4096)["body_omitted"] is True
    malformed = CapturedResponse(200, {"content-type": "application/json"},
                                 b'{"token":"do-not-show", broken')
    assert malformed.summary(body_max_chars=4096)["body_omitted"] is True
    assert "do-not-show" not in str(malformed.summary(body_max_chars=4096))


def test_html_preview_hides_hidden_tokens_and_script_credentials():
    response = CapturedResponse(200, {"content-type": "text/html"}, b'''
        <input name="csrf" value="hidden-secret"><meta name="csrf-token" content="meta-secret">
        <script>const account = {"token":"script-secret"};</script><p>reflected-marker</p>
    ''')
    summary = response.summary(body_max_chars=4096)
    for secret in ("hidden-secret", "meta-secret", "script-secret"):
        assert secret not in str(summary)
    assert "reflected-marker" in summary["body_preview"]


async def test_configured_and_live_tool_context_reach_conversation_separately():
    model = _ScriptedDecisionLLM([{"mode": "answer", "response": "목록 확인"}])
    router = InputRouter(ConversationAssistant(model))
    config = SimpleNamespace(capability=SimpleNamespace(mcp_servers=["configured"]),
                             engagement=SimpleNamespace(metadata={}))
    with environment_context({"REDTEAM_MCP_CONFIGURED_KIND": "web_api",
                              "REDTEAM_MCP_CONFIGURED_TRANSPORT": "stdio",
                              "REDTEAM_MCP_CONFIGURED_COMMAND": "not-invoked",
                              "REDTEAM_MCP_CONFIGURED_ALLOWED_TOOLS_JSON": '["http_request"]'}):
        context = configured_tool_context(config)
    await router.route("MCP 목록", context="idle", active=False, engagement_name="lab", scope=[],
                       **context)
    def payload(index):
        raw = json.loads(model.structured_calls[index]["messages"][0]["content"])
        return raw
    assert payload(0)["tool_availability"]["servers"][0]["server_id"] == "configured"
    assert payload(0)["tool_availability"]["state"] == "configured_unverified"
    descriptor = SimpleNamespace(server_id="configured", tool_name="http_request", operations=[],
                                 health_status=SimpleNamespace(value="healthy"))
    live = live_tool_context(SimpleNamespace(all=lambda: [descriptor]))
    await router.route("MCP 목록", context="active", active=True, engagement_name="lab", scope=[],
                       **live)
    assert payload(1)["tools"] == ["configured/http_request"]
    assert payload(1)["tool_availability"]["state"] == "discovered"


async def test_live_operator_route_overrides_declarations_with_finalized_catalog():
    from tests.unit.test_cli_operator_console import _console
    console, _, gateway, _, _, _ = await _console()
    descriptor = SimpleNamespace(server_id="shell-workroom", tool_name="shell_run", operations=[],
                                 health_status=SimpleNamespace(value="healthy"))
    console._deps.tool_catalog = SimpleNamespace(all=lambda: [descriptor])
    console._deps.engagement = SimpleNamespace(metadata={})
    console._deps.settings = SimpleNamespace(llm_request_policy=None)
    model = _ScriptedDecisionLLM([{"mode": "answer", "response": "연결 목록"}])
    await console.route_conversation(
        InputRouter(ConversationAssistant(model)), "어떤 도구?", context="active", active=True,
        engagement_name="lab", scope=[], tools=[],
        tool_availability={"state": "configured_unverified"},
    )
    payload = json.loads(model.structured_calls[0]["messages"][0]["content"])
    assert payload["tools"] == ["shell-workroom/shell_run"]
    assert payload["tool_availability"]["state"] == "discovered"
    assert gateway.calls == []


def gated_config():
    capacity = {"max_flow_init_deliveries": 10, "max_output_bytes": 65536}
    caps = {**capacity, "duration_seconds": 30}
    return {
        "budget": {"global_capacity": capacity, "target_capacity": capacity,
                   "install_capacity": {"max_flow_init_deliveries": 0, "max_output_bytes": 0}},
        "default_target_caps": {**caps, "purpose": "target"},
        "default_install_caps": {**caps, "purpose": "install"},
        "registry": {"targets": {"127.0.0.1": ["127.0.0.1"]}},
        "sidecar_image": "sha256:" + "1" * 64,
    }


async def test_kali_selection_uses_digest_bound_snapshot_and_rejects_edits(tmp_path):
    source = tmp_path / "gate.json"
    source.write_text(json.dumps(gated_config()))
    store = save(tmp_path / "lab.yaml", kali_network_mode="configured",
                 kali_network_policy_path=str(source))
    _, env = await prepared(store)
    snapshot = Path(env["REDTEAM_GATED_NETWORK_POLICY_PATH"])
    assert snapshot != source and snapshot.read_bytes() == source.read_bytes()
    assert snapshot.stat().st_mode & 0o077 == 0
    source.write_text("{}")
    with environment_context({}), pytest.raises(ValueError, match="kali_network_policy_changed"):
        await prepare_web_deployment(store.path)
    assert snapshot.read_bytes() != source.read_bytes()


@pytest.mark.parametrize("change", [
    {"registry": {"targets": {"other.test": ["127.0.0.1"]}}},
    {"sidecar_image": "unpinned:latest"},
])
def test_invalid_kali_policy_is_rejected_at_preview(tmp_path, change):
    source = tmp_path / "gate.json"
    source.write_text(json.dumps({**gated_config(), **change}))
    store = save(tmp_path / "lab.yaml")
    with pytest.raises(EngagementError):
        store.preview({**DRAFT, "kali_network_mode": "configured",
                       "kali_network_policy_path": str(source)})


async def test_kali_ip_cannot_escape_selected_origin_even_with_broad_scope(tmp_path):
    source = tmp_path / "gate.json"
    config = gated_config()
    config["registry"]["targets"]["127.0.0.1"] = ["192.0.2.3"]
    source.write_text(json.dumps(config))
    store = save(tmp_path / "lab.yaml", allowed_networks=["0.0.0.0/0"],
                 kali_network_mode="configured", kali_network_policy_path=str(source))
    with environment_context({}), pytest.raises(ValueError, match="address_mismatch"):
        await prepare_web_deployment(store.path)
