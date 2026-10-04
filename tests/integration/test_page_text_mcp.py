"""Independent in-process MCP and Gateway checks; no external web requests."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.server import MCPServer

from redteam.capability.adapters.mcp.transport import (
    RemoteCallResult,
    RemoteTool,
    StdioMCPTransport,
)
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.servers.page_text import PageTextConfig, PageTextReader
from redteam.capability.servers.page_text_mcp import create_server, load_page_text_config
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from tests.integration.test_page_text_transport import _certificate

HOST = "https://docs.example"
PUBLIC_IP = "93.184.215.14"


def _config_file(path: Path, **updates: object) -> Path:
    path.write_text(json.dumps({"allowed_origins": [HOST], **updates}), encoding="utf-8")
    return path


def _reader(
    config: PageTextConfig,
    handler: httpx.MockTransport,
) -> PageTextReader:
    async def resolve(host: str, port: int) -> tuple[str, ...]:
        return (PUBLIC_IP,)

    return PageTextReader(config, resolver=resolve, transport_factory=lambda: handler)


def _settings(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    prefix = "REDTEAM_MCP_PAGE_TEXT"
    monkeypatch.setenv(f"{prefix}_KIND", "web_api")
    monkeypatch.setenv(f"{prefix}_TRANSPORT", "stdio")
    monkeypatch.setenv(f"{prefix}_COMMAND", sys.executable)
    monkeypatch.setenv(f"{prefix}_RISK_LEVEL", "low")
    monkeypatch.setenv(f"{prefix}_PROFILE", "page_text_readonly")
    monkeypatch.setenv(f"{prefix}_PAGE_TEXT_CONFIG", str(path))
    # Stdio child must import the same worktree source as the parent so
    # discovery schemas stay in lockstep (fail-closed on version skew).
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    monkeypatch.setenv("PYTHONPATH", repo_src)
    monkeypatch.setenv(f"{prefix}_PASSTHROUGH_ENV", "PYTHONPATH")


class InProcessTransport:
    """Use the official MCP Client against the actual server object."""

    def __init__(self, server: MCPServer, *, corrupt_output: bool = False) -> None:
        self.server = server
        self.corrupt_output = corrupt_output

    async def list_tools(self) -> list[RemoteTool]:
        async with Client(self.server) as client:
            listing = await client.list_tools()
        return [
            RemoteTool(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
                output_schema=(dict(tool.output_schema) if tool.output_schema else None),
            )
            for tool in listing.tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict) -> RemoteCallResult:
        async with Client(self.server) as client:
            result = await client.call_tool(tool_name, arguments)
        output = dict(result.structured_content) if result.structured_content else None
        if self.corrupt_output and not result.is_error:
            output = {"status_code": "wrong shape"}
        return RemoteCallResult(is_error=bool(result.is_error), output=output)

    async def health_check(self) -> bool:
        return True


def _invocation(url: str = HOST) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId("inv_page_read"),
        action_id=ActionId("action_page_read"),
        run_id=RunId("run_page_read"),
        tool_id=ToolId("web.fetch_page_text"),
        operation="page_read",
        arguments={"url": url},
        timeout_seconds=3,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_page_read"), run_id=RunId("run_page_read")
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_actual_mcp_success_denial_bad_input_and_timeout() -> None:
    sent: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/slow":
            await asyncio.sleep(1)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              stream=httpx.ByteStream("<title>읽기</title><p>본문</p>".encode()))

    config = PageTextConfig(allowed_origins=(HOST,), timeout_seconds=0.05)
    server = create_server(config, reader=_reader(config, httpx.MockTransport(handle)))
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == ["fetch_page_text"]
        assert tools[0].input_schema["properties"]["max_chars"]["anyOf"][0]["maximum"] == 20000
        success = await client.call_tool("fetch_page_text", {"url": HOST + "/ok"})
        assert success.is_error is False
        assert success.structured_content["title"] == "읽기"
        assert success.structured_content["text"] == "본문"
        denied = await client.call_tool("fetch_page_text", {"url": "https://blocked.example/"})
        assert denied.is_error is True
        assert "access_denied" in str(denied.content[0])
        attempted_override = await client.call_tool("fetch_page_text", {
            "url": "https://blocked.example/",
            "allowed_origins": ["https://blocked.example"],
        })
        assert attempted_override.is_error is True
        bad = await client.call_tool("fetch_page_text", {"url": "http://docs.example/"})
        assert bad.is_error is True
        assert "invalid_input" in str(bad.content[0])
        oversized = await client.call_tool("fetch_page_text", {
            "url": HOST, "max_chars": config.max_return_chars + 1,
        })
        assert oversized.is_error is True
        timed_out = await client.call_tool("fetch_page_text", {"url": HOST + "/slow"})
        assert timed_out.is_error is True
        assert "timeout" in str(timed_out.content[0])
    assert len(sent) == 2


async def test_actual_stdio_bootstrap_discovery_and_default_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "page-policy.json", allowed_origins=[])
    _settings(monkeypatch, path)
    gateway = await build_mcp_tool_gateway(["page-text"], health_refresh_enabled=False)
    descriptor = gateway.get(ToolId("web.fetch_page_text"))
    assert descriptor is not None
    assert descriptor.operations == ["page_read"] and descriptor.target_types == ["url"]
    result = await gateway.invoke(_invocation())
    assert not result.success
    assert result.error is not None and result.error.code == "mcp_tool_error"
    assert "access_denied" in str(result.output)
    assert result.action_id == ActionId("action_page_read")
    assert result.invocation_id == InvocationId("inv_page_read")


async def test_actual_stdio_gateway_reads_local_https_and_preserves_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real stdio and TLS; the public-IP-to-loopback remap lives in a test-only child."""
    server_context, _ = _certificate(tmp_path)
    certificate = tmp_path / "cert.pem"
    sni: list[str | None] = []
    requests: list[bytes] = []
    server_context.set_servername_callback(lambda _sock, host, _context: sni.append(host))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            body = b"<title>Local docs</title><p>Verified page text</p>"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        policy_path = _config_file(
            tmp_path / "page-policy.json",
            allowed_origins=[f"https://docs.example:{port}"],
            max_return_chars=100,
        )
        _settings(monkeypatch, policy_path)
        settings = MCPServerSettings.from_environment("page-text")
        fixture = Path(__file__).with_name("_page_text_stdio_local.py")

        def local_stdio(_settings: MCPServerSettings) -> StdioMCPTransport:
            repo_src = str(Path(__file__).resolve().parents[2] / "src")
            import os as _os
            child_env = {"PYTHONPATH": repo_src}
            parent_pythonpath = _os.environ.get("PYTHONPATH")
            if parent_pythonpath and parent_pythonpath != repo_src:
                child_env["PYTHONPATH"] = repo_src + _os.pathsep + parent_pythonpath
            return StdioMCPTransport(
                sys.executable,
                args=(
                    str(fixture), "--config", settings.page_text_config_path or "",
                    "--sha256", settings.page_text_config_sha256 or "",
                    "--port", str(port), "--certificate", str(certificate),
                ),
                env=child_env,
            )

        monkeypatch.setattr("redteam.capability.bootstrap._create_transport", local_stdio)
        gateway = await build_mcp_tool_gateway(["page-text"], health_refresh_enabled=False)
        descriptor = gateway.get(ToolId("web.fetch_page_text"))
        assert descriptor is not None and descriptor.output_schema is not None
        invocation = _invocation(f"https://docs.example:{port}/guide").model_copy(
            update={"timeout_seconds": 10}
        )
        result = await gateway.invoke(invocation)
        assert result.success, result.error
        assert isinstance(result.output, dict)
        Draft202012Validator(descriptor.output_schema).validate(result.output)
        assert result.output == {
            "requested_url": f"https://docs.example:{port}/guide",
            "final_url": f"https://docs.example:{port}/guide",
            "status_code": 200,
            "content_type": "text/html",
            "title": "Local docs",
            "text": "Verified page text",
            "truncated": False,
            "redirect_url": None,
            "body_present": True,
        }
        assert result.action_id == invocation.action_id
        assert result.invocation_id == invocation.invocation_id
        assert result.tool_id == ToolId("web.fetch_page_text")
        assert sni == ["docs.example"]
        assert len(requests) == 1
        assert f"Host: docs.example:{port}\r\n".encode() in requests[0]


async def test_settings_discovery_gateway_ids_and_output_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "page-policy.json", max_return_chars=7)
    _settings(monkeypatch, path)
    settings = MCPServerSettings.from_environment("page-text")
    assert settings.allowed_tools == ("fetch_page_text",)
    assert settings.tool_operations == {"fetch_page_text": ("page_read",)}
    assert settings.args[-2:] == ("--sha256", settings.page_text_config_sha256)
    config, _ = load_page_text_config(path, settings.page_text_config_sha256)
    transport = InProcessTransport(create_server(config, reader=_reader(
            config, httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"public text")
        )),
    )))
    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", lambda _: transport)
    gateway = await build_mcp_tool_gateway(["page-text"], health_refresh_enabled=False)
    descriptor = gateway.get(ToolId("web.fetch_page_text"))
    assert descriptor is not None
    assert descriptor.operations == ["page_read"]
    assert descriptor.target_types == ["url"]
    assert descriptor.input_schema["properties"]["max_chars"]["anyOf"][0]["maximum"] == 7
    assert [tool.tool_id for tool in gateway.list_tools_for_action("web", "page_read")] == [
        descriptor.tool_id
    ]
    result = await gateway.invoke(_invocation())
    assert isinstance(result.output, dict)
    assert result.success and result.output["text"] == "public "
    assert result.output["truncated"] is True
    assert result.action_id == ActionId("action_page_read")
    assert result.invocation_id == InvocationId("inv_page_read")
    assert result.tool_id == ToolId("web.fetch_page_text")


async def test_discovery_and_result_schema_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "page-policy.json", max_return_chars=4)
    _settings(monkeypatch, path)
    config, _ = load_page_text_config(path)
    server = create_server(config, reader=_reader(config, httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"okay")
        )
    )))
    transport = InProcessTransport(server)
    original_list = transport.list_tools

    async def broken_list() -> list[RemoteTool]:
        tools = await original_list()
        tools[0].input_schema["properties"]["max_chars"]["anyOf"][0]["maximum"] = 999
        return tools

    transport.list_tools = broken_list  # type: ignore[method-assign]
    monkeypatch.setattr("redteam.capability.bootstrap._create_transport", lambda _: transport)
    with pytest.raises(MCPConfigurationError, match="page text discovery"):
        await build_mcp_tool_gateway(["page-text"], health_refresh_enabled=False)

    bad_result_transport = InProcessTransport(server, corrupt_output=True)
    monkeypatch.setattr(
        "redteam.capability.bootstrap._create_transport", lambda _: bad_result_transport
    )
    gateway = await build_mcp_tool_gateway(["page-text"], health_refresh_enabled=False)
    result = await gateway.invoke(_invocation())
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "tool_output_schema_mismatch"
    assert result.action_id == ActionId("action_page_read")
    assert result.invocation_id == InvocationId("inv_page_read")


def test_settings_share_one_operator_file_and_pin_its_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "page-policy.json", max_return_chars=5)
    _settings(monkeypatch, path)
    settings = MCPServerSettings.from_environment("page-text")
    assert settings.args[3] == str(path)
    with pytest.raises(ValueError, match="changed after bootstrap"):
        path.write_text('{"allowed_origins":[]}', encoding="utf-8")
        load_page_text_config(path, settings.page_text_config_sha256)


def test_page_profile_rejects_other_tools_and_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _config_file(tmp_path / "page-policy.json")
    _settings(monkeypatch, path)
    monkeypatch.setenv("REDTEAM_MCP_PAGE_TEXT_ALLOWED_TOOLS_JSON", '["other"]')
    with pytest.raises(MCPConfigurationError, match="exact tool allowlist"):
        MCPServerSettings.from_environment("page-text")
    monkeypatch.setenv("REDTEAM_MCP_PAGE_TEXT_ALLOWED_TOOLS_JSON", '["fetch_page_text"]')
    monkeypatch.setenv(
        "REDTEAM_MCP_PAGE_TEXT_TOOL_OPERATIONS_JSON",
        '{"fetch_page_text":["http_request"]}',
    )
    with pytest.raises(MCPConfigurationError, match="exact operation binding"):
        MCPServerSettings.from_environment("page-text")
