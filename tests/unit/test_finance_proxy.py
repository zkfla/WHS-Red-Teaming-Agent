"""Product-neutral proxy capture import and bounded replay behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.adapters.mcp.transport import remote_tool_metadata
from redteam.capability.servers import proxy
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ToolInputError
from redteam.core.contracts.tool import (
    PROVIDER_METADATA_RESOURCE_BINDINGS,
    ToolDescriptor,
    ToolResourceBinding,
)
from redteam.core.ids import ToolId
from redteam.core.tool_resources import ResourceBindingError, resource_identities


@pytest.fixture
def policy() -> ServerPolicy:
    return ServerPolicy.model_validate(
        {
            "targets": {
                "bank": {
                    "origin": "https://bank.test",
                    "allowed_paths": ["/account", "/validate"],
                    "allowed_path_methods": {"/validate": ["POST"]},
                    "replayable_paths": ["/account", "/validate"],
                    "mutable_json_fields": {"/validate": ["alias", "amount"]},
                }
            },
            "allowed_networks": ["192.0.2.0/24"],
            "sessions": {
                "alice": {
                    "target_ref": "bank",
                    "principal_ref": "alice",
                    "bearer_env": "TOKEN_A",
                },
                "bob": {
                    "target_ref": "bank",
                    "principal_ref": "bob",
                    "bearer_env": "TOKEN_B",
                },
            },
            "proxy_replay_enabled": True,
        }
    )


@pytest.fixture
def capture_path(tmp_path: Path) -> Path:
    entries = [
        {
            "request": {
                "method": "GET",
                "url": "https://bank.test/account",
                "headers": [
                    {"name": "Authorization", "value": "Bearer captured-secret"},
                    {"name": "Cookie", "value": "captured-cookie"},
                ],
            },
            "response": {
                "status": 200,
                "headers": [{"name": "Set-Cookie", "value": "response-secret"}],
                "content": {"text": "sensitive response body"},
            },
        },
        {
            "request": {
                "method": "POST",
                "url": "https://bank.test/validate",
                "headers": [],
                "postData": {
                    "mimeType": "application/json; charset=utf-8",
                    "text": json.dumps({"alias": "synthetic", "amount": 1000}),
                },
            },
            "response": {
                "status": 200,
                "headers": [],
                "content": {"text": "synthetic result"},
            },
        },
        {
            "request": {
                "method": "GET",
                "url": "https://outside.test/analytics",
                "headers": [],
            },
            "response": {"status": 204, "headers": [], "content": {"text": ""}},
        },
    ]
    path = tmp_path / "capture.har"
    path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
    return path


def test_zap_empty_post_data_and_unreceived_site_tree_entries(policy: ServerPolicy) -> None:
    entries = [{
        "request": {
            "method": "GET", "url": "https://bank.test/account", "headers": [],
            "postData": {"mimeType": "", "text": "", "params": []},
        },
        "response": {"status": status, "content": {"text": ""}},
    } for status in (200, 0)]
    backend = proxy.HarCaptureBackend.from_bytes(
        json.dumps({"log": {"entries": entries}}).encode(), policy,
    )
    assert len(backend.list_flows()) == 1
    assert backend.ignored_flow_count == 1
    assert backend.list_flows()[0].request_json is None


def test_har_import_discards_auth_raw_bodies_and_out_of_scope_flows(
    policy: ServerPolicy,
    capture_path: Path,
) -> None:
    backend = proxy.HarCaptureBackend.load(str(capture_path), policy)
    flows = backend.list_flows()
    exported = str([flow.summary() for flow in flows])

    assert len(flows) == 2
    assert backend.ignored_flow_count == 1
    assert "captured-secret" not in exported
    assert "captured-cookie" not in exported
    assert "response-secret" not in exported
    assert "sensitive response body" not in exported
    assert flows[1].summary()["request_json_fields"] == ["alias", "amount"]


def test_har_import_rejects_query_variants_and_encoded_path_aliases(
    policy: ServerPolicy,
) -> None:
    entries = [{
        "request": {"method": "GET", "url": url, "headers": []},
        "response": {"status": 200, "headers": [], "content": {"text": "safe"}},
    } for url in (
        "https://bank.test/account?owner=alice",
        "https://bank.test/account?owner=bob",
        "https://bank.test/account%2Fadmin",
    )]
    backend = proxy.HarCaptureBackend.from_bytes(
        json.dumps({"log": {"entries": entries}}).encode(), policy,
    )

    assert backend.list_flows() == []
    assert backend.ignored_flow_count == len(entries)


def test_invalid_base64_response_is_rejected(
    policy: ServerPolicy,
    capture_path: Path,
) -> None:
    document = json.loads(capture_path.read_text(encoding="utf-8"))
    document["log"]["entries"][0]["response"]["content"] = {
        "text": "not-base64!",
        "encoding": "base64",
    }
    capture_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ToolInputError, match="invalid captured response"):
        proxy.HarCaptureBackend.load(str(capture_path), policy)


async def test_proxy_tools_replay_only_allowlisted_shape_with_configured_sessions(
    policy: ServerPolicy,
    capture_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_request(*args: Any, **kwargs: Any) -> CapturedResponse:
        calls.append(
            {
                "target_ref": args[1],
                "path": args[2],
                "method": kwargs.get("method", "GET"),
                "session_ref": kwargs.get("session_ref"),
                "json_body": kwargs.get("json_body"),
            }
        )
        status = 403 if kwargs.get("session_ref") == "bob" else 200
        return CapturedResponse(status, {}, b"never returned raw")

    monkeypatch.setattr(proxy, "request", fake_request)
    async with Client(proxy.create_server(policy, str(capture_path))) as client:
        listed = await client.call_tool("proxy_list_flows", {})
        assert listed.structured_content is not None
        flows = listed.structured_content["flows"]
        read_ref = next(flow["flow_ref"] for flow in flows if flow["method"] == "GET")
        write_ref = next(flow["flow_ref"] for flow in flows if flow["method"] == "POST")

        replayed = await client.call_tool(
            "proxy_replay_flow", {"flow_ref": read_ref, "session_ref": "alice"}
        )
        mutated = await client.call_tool(
            "proxy_mutate_json",
            {
                "flow_ref": write_ref,
                "session_ref": "alice",
                "mutations": {"amount": 2000},
            },
        )
        rejected = await client.call_tool(
            "proxy_mutate_json", {"flow_ref": write_ref, "mutations": {"admin": True}}
        )
        missing_session = await client.call_tool("proxy_replay_flow", {"flow_ref": write_ref})
        compared = await client.call_tool(
            "proxy_compare_sessions",
            {
                "flow_ref": read_ref,
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            },
        )

    assert replayed.structured_content is not None
    assert replayed.structured_content["body_omitted"] is True
    assert mutated.structured_content is not None
    assert mutated.structured_content["mutated_fields"] == ["amount"]
    assert rejected.is_error is True
    assert missing_session.is_error is True
    assert compared.structured_content is not None
    assert compared.structured_content["verdict"] == "observation_only"
    assert compared.structured_content["request_count"] == 2
    assert calls == [
        {
            "target_ref": "bank",
            "path": "/account",
            "method": "GET",
            "session_ref": "alice",
            "json_body": None,
        },
        {
            "target_ref": "bank",
            "path": "/validate",
            "method": "POST",
            "session_ref": "alice",
            "json_body": {"alias": "synthetic", "amount": 2000},
        },
        {
            "target_ref": "bank",
            "path": "/account",
            "method": "GET",
            "session_ref": "alice",
            "json_body": None,
        },
        {
            "target_ref": "bank",
            "path": "/account",
            "method": "GET",
            "session_ref": "bob",
            "json_body": None,
        },
    ]


async def test_independent_proxy_paths_overlap_within_policy_capacity(
    policy: ServerPolicy,
    capture_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = policy.model_copy(update={"max_concurrency": 2})
    condition = asyncio.Condition()
    release = asyncio.Event()
    active = peak = started = 0

    async def fake_request(*args: Any, **kwargs: Any) -> CapturedResponse:
        nonlocal active, peak, started
        async with condition:
            active += 1
            started += 1
            peak = max(peak, active)
            condition.notify_all()
            await condition.wait_for(lambda: started >= 2)
        await release.wait()
        async with condition:
            active -= 1
        return CapturedResponse(200, {}, b"synthetic")

    monkeypatch.setattr(proxy, "request", fake_request)
    async with Client(proxy.create_server(configured, str(capture_path))) as client:
        flows = (await client.call_tool("proxy_list_flows", {})).structured_content
        assert flows is not None
        read_ref = next(flow["flow_ref"] for flow in flows["flows"] if flow["method"] == "GET")
        write_ref = next(flow["flow_ref"] for flow in flows["flows"] if flow["method"] == "POST")
        calls = [
            asyncio.create_task(client.call_tool(
                "proxy_replay_flow", {"flow_ref": read_ref, "session_ref": "alice"},
            )),
            asyncio.create_task(client.call_tool(
                "proxy_replay_flow", {"flow_ref": write_ref, "session_ref": "alice"},
            )),
        ]
        try:
            async with condition:
                await asyncio.wait_for(condition.wait_for(lambda: started == 2), 2)
        finally:
            release.set()
        results = await asyncio.gather(*calls)

    assert peak == 2
    assert all(not result.is_error for result in results)


async def test_proxy_same_origin_path_serializes_session_aliases(
    policy: ServerPolicy,
    capture_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = policy.model_copy(update={
        "max_concurrency": 2,
        "sessions": {**policy.sessions, "alice-alias": policy.sessions["alice"]},
    })
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    calls_started = 0

    async def fake_request(*args: Any, **kwargs: Any) -> CapturedResponse:
        nonlocal calls_started
        calls_started += 1
        if calls_started == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return CapturedResponse(200, {}, b"synthetic")

    monkeypatch.setattr(proxy, "request", fake_request)
    async with Client(proxy.create_server(configured, str(capture_path))) as client:
        flows = (await client.call_tool("proxy_list_flows", {})).structured_content
        assert flows is not None
        read_ref = next(flow["flow_ref"] for flow in flows["flows"] if flow["method"] == "GET")
        first = asyncio.create_task(client.call_tool(
            "proxy_replay_flow", {"flow_ref": read_ref, "session_ref": "alice"},
        ))
        await first_started.wait()
        second = asyncio.create_task(client.call_tool(
            "proxy_replay_flow", {"flow_ref": read_ref, "session_ref": "alice-alias"},
        ))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second_started.wait(), 0.05)
        finally:
            release_first.set()
        results = await asyncio.gather(first, second)

    assert calls_started == 2
    assert all(not result.is_error for result in results)


def test_proxy_resource_identity_collapses_origin_aliases(
    policy: ServerPolicy,
) -> None:
    alias_policy = policy.model_copy(update={
        "targets": {
            **policy.targets,
            "bank-alias": policy.targets["bank"].model_copy(
                update={"origin": "HTTPS://BANK.TEST:443/"},
            ),
        },
    })
    flow = proxy.ProxyFlow(
        flow_ref="flow_a", target_ref="bank", method="GET", path="/account",
        request_json=None, captured_response=CapturedResponse(200, {}, b""),
    )
    alias_flow = proxy.ProxyFlow(
        flow_ref="flow_b", target_ref="bank-alias", method="POST", path="/account",
        request_json=None, captured_response=CapturedResponse(200, {}, b""),
    )

    assert proxy._proxy_resource_key(alias_policy, flow) == proxy._proxy_resource_key(
        alias_policy, alias_flow,
    )
    assert proxy._proxy_resource_identity(alias_policy, flow) == (
        proxy._proxy_resource_identity(alias_policy, alias_flow)
    )
    idna_policy = alias_policy.model_copy(update={
        "targets": {
            **alias_policy.targets,
            "unicode-origin": alias_policy.targets["bank"].model_copy(
                update={"origin": "HTTPS://BÜCHER.TEST.:443/"},
            ),
            "ascii-origin": alias_policy.targets["bank"].model_copy(
                update={"origin": "https://xn--bcher-kva.test"},
            ),
        },
    })
    unicode_flow = proxy.ProxyFlow(
        flow_ref="unicode_flow", target_ref="unicode-origin", method=flow.method,
        path=flow.path, request_json=flow.request_json,
        captured_response=flow.captured_response,
    )
    ascii_flow = proxy.ProxyFlow(
        flow_ref="ascii_flow", target_ref="ascii-origin", method=alias_flow.method,
        path=alias_flow.path, request_json=alias_flow.request_json,
        captured_response=alias_flow.captured_response,
    )
    assert proxy._proxy_resource_key(idna_policy, unicode_flow) == (
        proxy._proxy_resource_key(idna_policy, ascii_flow)
    )
    port_zero_policy = alias_policy.model_copy(update={
        "targets": {
            **alias_policy.targets,
            "port-zero": alias_policy.targets["bank"].model_copy(
                update={"origin": "https://bank.test:0"},
            ),
        },
    })
    port_zero_flow = proxy.ProxyFlow(
        flow_ref="port_zero", target_ref="port-zero", method="GET", path=flow.path,
        request_json=None, captured_response=flow.captured_response,
    )
    assert proxy._proxy_resource_key(port_zero_policy, port_zero_flow)[2] == 0
    assert proxy._proxy_resource_key(port_zero_policy, port_zero_flow) != (
        proxy._proxy_resource_key(port_zero_policy, flow)
    )


def test_proxy_resource_value_map_respects_har_and_contract_limits(
    policy: ServerPolicy,
) -> None:
    configured = policy.model_copy(update={"proxy_max_flows": 1000})
    entry = {
        "request": {"method": "GET", "url": "https://bank.test/account", "headers": []},
        "response": {"status": 200, "headers": [], "content": {"text": "safe"}},
    }
    backend = proxy.HarCaptureBackend.from_bytes(
        json.dumps({"log": {"entries": [entry] * configured.proxy_max_flows}}).encode(),
        configured,
    )
    (binding_row,) = proxy._proxy_resource_bindings(configured, backend)

    assert len(binding_row["value_map"]) == configured.proxy_max_flows == 1000
    ToolResourceBinding(**binding_row)


async def test_proxy_resource_metadata_tracks_canonical_paths_and_fails_closed_after_reload(
    policy: ServerPolicy,
    capture_path: Path,
) -> None:
    initial = proxy.HarCaptureBackend.load(str(capture_path), policy)
    async with Client(proxy.create_server(policy, str(capture_path))) as client:
        tools = (await client.list_tools()).tools
    replay_tools = {
        tool.name: tool for tool in tools
        if tool.name in {"proxy_replay_flow", "proxy_mutate_json", "proxy_compare_sessions"}
    }
    assert set(replay_tools) == {
        "proxy_replay_flow", "proxy_mutate_json", "proxy_compare_sessions",
    }
    metadata = remote_tool_metadata(replay_tools["proxy_replay_flow"])
    binding_rows = metadata[PROVIDER_METADATA_RESOURCE_BINDINGS]
    assert len(binding_rows) == 1
    binding_row = binding_rows[0]
    assert binding_row["namespace"] == "proxy.replay-path"
    descriptor = ToolDescriptor(
        tool_id=ToolId("web.proxy.replay_flow"), server_id="web-proxy",
        tool_name="proxy_replay_flow", input_schema=replay_tools["proxy_replay_flow"].input_schema,
        resource_bindings=[ToolResourceBinding(**binding_row)],
    )
    flows = initial.list_flows()
    read_flow = next(flow for flow in flows if flow.method == "GET")
    write_flow = next(flow for flow in flows if flow.method == "POST")

    alice_identity = resource_identities(
        descriptor, {"flow_ref": read_flow.flow_ref, "session_ref": "alice"},
    )
    bob_identity = resource_identities(
        descriptor, {"flow_ref": read_flow.flow_ref, "session_ref": "bob"},
    )
    assert alice_identity == bob_identity
    assert alice_identity != resource_identities(descriptor, {"flow_ref": write_flow.flow_ref})
    assert all(
        remote_tool_metadata(tool)[PROVIDER_METADATA_RESOURCE_BINDINGS] == binding_rows
        for tool in replay_tools.values()
    )

    document = json.loads(capture_path.read_text(encoding="utf-8"))
    document["log"]["entries"].append(document["log"]["entries"][0])
    reloaded = proxy.HarCaptureBackend.from_bytes(json.dumps(document).encode(), policy)
    newly_added_ref = reloaded.list_flows()[-1].flow_ref
    assert newly_added_ref not in binding_row["value_map"]
    with pytest.raises(ResourceBindingError):
        resource_identities(descriptor, {"flow_ref": newly_added_ref})


async def test_replay_switch_blocks_before_network(
    policy: ServerPolicy,
    capture_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("disabled replay must not call the target")

    monkeypatch.setattr(proxy, "request", fail)
    disabled = policy.model_copy(update={"proxy_replay_enabled": False})
    backend = proxy.HarCaptureBackend.load(str(capture_path), disabled)
    read_ref = next(flow.flow_ref for flow in backend.list_flows() if flow.method == "GET")

    async with Client(proxy.create_server(disabled, str(capture_path))) as client:
        result = await client.call_tool("proxy_replay_flow", {"flow_ref": read_ref})
    assert result.is_error is True


async def test_operator_capture_reload_replaces_catalog_without_target_traffic(
    policy: ServerPolicy,
    capture_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("capture reload must not call the target")

    monkeypatch.setattr(proxy, "request", fail)
    async with Client(proxy.create_server(policy, str(capture_path))) as client:
        document = json.loads(capture_path.read_text(encoding="utf-8"))
        document["log"]["entries"] = document["log"]["entries"][:1]
        capture_path.write_text(json.dumps(document), encoding="utf-8")
        reloaded = await client.call_tool("proxy_reload_capture", {})
        listed = await client.call_tool("proxy_list_flows", {})

    assert reloaded.structured_content is not None
    assert reloaded.structured_content["reloaded"] is True
    assert reloaded.structured_content["flow_count"] == 1
    assert listed.structured_content is not None
    assert len(listed.structured_content["flows"]) == 1
