"""Actual MCP wire tests for all Python server roles, using only synthetic responses."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.servers import active, recon, web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ResolvedTarget


@pytest.fixture
def policy() -> ServerPolicy:
    return ServerPolicy.model_validate(
        {
            "targets": {
                "bank": {
                    "origin": "http://127.0.0.1:8080",
                    "allowed_paths": ["/account", "/openapi.json", "/oidc"],
                }
            },
            "allowed_networks": ["127.0.0.0/8"],
            "sessions": {
                "alice": {"target_ref": "bank", "principal_ref": "a", "bearer_env": "TOKEN_A"},
                "bob": {"target_ref": "bank", "principal_ref": "b", "bearer_env": "TOKEN_B"},
            },
        }
    )


@pytest.mark.parametrize(
    "role,expected",
    [
        ("web-http", {"session_list", "http_request", "http_compare_sessions"}),
        (
            "recon-passive",
            {"dns_resolve", "http_probe", "tls_inspect", "openapi_inventory", "oidc_inventory"},
        ),
        ("recon-active", {"active_status", "port_scan"}),
    ],
)
async def test_real_stdio_roles_discover_and_reject_unconfigured_target(
    policy: ServerPolicy,
    tmp_path: Path,
    role: str,
    expected: set[str],
) -> None:
    path = tmp_path / "policy.json"
    path.write_text(policy.model_dump_json())
    transport = StdioMCPTransport(
        sys.executable,
        args=[
            "-m",
            "redteam.capability.servers",
            role,
            "--policy",
            str(path),
        ],
    )
    tools = await transport.list_tools()
    assert {tool.name for tool in tools} == expected
    operation = {
        "web-http": "http_request",
        "recon-passive": "http_probe",
        "recon-active": "port_scan",
    }[role]
    arguments: dict[str, Any] = {"target_ref": "not-configured"}
    if role != "recon-active":
        arguments["path"] = "/account"
    result = await transport.call_tool(operation, arguments)
    assert result.is_error is True


async def test_two_principal_comparison_is_observation_not_finding(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_request(*args: Any, **kwargs: Any) -> CapturedResponse:
        calls.append(kwargs["session_ref"])
        return CapturedResponse(200 if kwargs["session_ref"] == "alice" else 403, {}, b"synthetic")

    monkeypatch.setattr(web_http, "request", fake_request)
    async with Client(web_http.create_server(policy)) as client:
        result = await client.call_tool(
            "http_compare_sessions",
            {
                "target_ref": "bank",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            },
        )
    assert result.structured_content is not None
    assert result.structured_content["same_status"] is False
    assert result.structured_content["verdict"] == "observation_only"
    assert calls == ["alice", "bob"]


async def test_openapi_and_oidc_do_not_follow_advertised_urls(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_request(*args: Any, **kwargs: Any) -> CapturedResponse:
        calls.append(args[2])
        document = {
            "openapi": "3.0.3",
            "paths": {"/accounts/{id}": {"get": {"deprecated": True}}},
            "servers": [{"url": "http://outside.test"}],
            "$ref": "http://outside.test/schema",
            "issuer": "http://outside.test",
            "jwks_uri": "http://outside.test/jwks",
            "code_challenge_methods_supported": ["S256"],
        }
        return CapturedResponse(200, {}, json.dumps(document).encode())

    monkeypatch.setattr(recon, "request", fake_request)
    async with Client(recon.create_server(policy)) as client:
        inventory = await client.call_tool(
            "openapi_inventory",
            {
                "target_ref": "bank",
                "path": "/openapi.json",
            },
        )
        oidc = await client.call_tool("oidc_inventory", {"target_ref": "bank", "path": "/oidc"})
    assert inventory.structured_content is not None
    assert inventory.structured_content["external_refs_followed"] is False
    assert inventory.structured_content["operations"][0]["deprecated"] is True
    assert oidc.structured_content is not None
    assert oidc.structured_content["endpoints_followed"] is False
    assert calls == ["/openapi.json", "/oidc"]


async def test_active_disabled_before_any_dns_or_process(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("disabled scan must not resolve targets or spawn processes")

    monkeypatch.setattr(active, "resolve_target", fail)
    monkeypatch.setattr(active, "run_nmap", fail)
    async with Client(active.create_server(policy)) as client:
        result = await client.call_tool("port_scan", {"target_ref": "bank"})
    assert result.is_error is True


async def test_active_scan_uses_validated_address_and_fixed_port_list(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(*args: Any) -> ResolvedTarget:
        return ResolvedTarget(policy.targets["bank"], "127.0.0.1", 8080, ("127.0.0.1",))

    async def scan(address: str, ports: list[int], timeout: float) -> bytes:
        assert address == "127.0.0.1" and ports == [8080]
        return (
            b'<nmaprun><host><ports><port protocol="tcp" portid="8080">'
            b'<state state="open"/></port></ports></host>'
            b'<runstats><finished exit="success"/></runstats></nmaprun>'
        )

    monkeypatch.setattr(active, "resolve_target", resolve)
    monkeypatch.setattr(active, "run_nmap", scan)
    enabled = policy.model_copy(update={"active_enabled": True, "active_ports": [8080]})
    async with Client(active.create_server(enabled)) as client:
        result = await client.call_tool("port_scan", {"target_ref": "bank"})
    assert result.structured_content is not None
    assert result.structured_content["ports"][0]["state"] == "open"


async def test_cancelled_scan_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = None
        killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    process = Process()

    async def create(*args: Any, **kwargs: Any) -> Process:
        assert "--script" not in args and "-sT" in args
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(active.run_nmap("127.0.0.1", [8080], 5))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed
