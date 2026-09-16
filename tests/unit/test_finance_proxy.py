"""Product-neutral proxy capture import and bounded replay behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.servers import proxy
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ToolInputError


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
