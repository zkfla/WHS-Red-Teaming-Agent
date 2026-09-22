"""New MCP endpoints run through the actual MCP client with bounded engine fixtures."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.servers import nuclei, zap
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse


def policy() -> ServerPolicy:
    return ServerPolicy.model_validate({
        "targets": {"bank": {"origin": "https://bank.test", "allowed_paths": ["/"]}},
        "allowed_networks": ["192.0.2.0/24"],
    })


async def test_zap_live_snapshot_filters_scope_and_credentials(monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def export(origin: str, base: str, key: str, limit: int, **kwargs: Any) -> bytes:
        calls.append((origin, base))
        assert key == "secret-key"
        return json.dumps({"log": {"entries": [{
            "request": {"method": "GET", "url": url, "headers": []},
            "response": {"status": 200, "headers": [], "content": {"text": "secret-body"}},
        } for url in ("https://bank.test/", "https://bank.test/forbidden", "https://other.test/")]}}).encode()

    monkeypatch.setattr(zap, "fetch_zap_har", export)
    monkeypatch.setenv("ZAP_KEY", "secret-key")
    async with Client(zap.create_server(policy(), "http://127.0.0.1:8080", "ZAP_KEY")) as client:
        result = await client.call_tool("zap_capture_snapshot", {"target_ref": "bank"})
        output = result.structured_content
        assert output is not None and len(output["flows"]) == 1
        assert output["coverage"] == "first_page_only" and output["target_request_count"] == 0
        assert "secret" not in str(output)
        assert (await client.call_tool("zap_capture_snapshot", {"target_ref": "other"})).is_error
    assert len(calls) == 1


async def test_nuclei_check_uses_scoped_fetch_and_sanitized_local_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    temporary_paths = []

    async def fetch(*args: Any, **kwargs: Any) -> CapturedResponse:
        calls.append(args[2])
        return CapturedResponse(200, {"set-cookie": "secret", "content-length": "99"}, b"secret")

    async def engine(binary: str, directory: Path, timeout: float) -> bytes:
        temporary_paths.append(directory)
        assert "secret" not in (directory / "response.txt").read_text()
        assert "Content-Length: 0" in (directory / "response.txt").read_text()
        return b'{"template-id":"missing-csp","response":"must-be-omitted"}\n'

    monkeypatch.setattr(nuclei.shutil, "which", lambda _: "/test/nuclei")
    monkeypatch.setattr(nuclei, "request", fetch)
    monkeypatch.setattr(nuclei, "_run_engine", engine)
    async with Client(nuclei.create_server(policy())) as client:
        invalid = await client.call_tool("nuclei_response_check", {
            "target_ref": "bank", "path": "/", "check_id": "unreviewed-template",
        })
        assert invalid.is_error and calls == []
        result = await client.call_tool("nuclei_response_check", {
            "target_ref": "bank", "path": "/", "check_id": "missing-csp",
        })
    assert result.structured_content is not None and result.structured_content["matched"]
    assert "must-be-omitted" not in str(result.structured_content)
    assert calls == ["/"] and not temporary_paths[0].exists()


async def test_nuclei_cancellation_kills_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class Process:
        returncode: int | None = None
        stdout: Any
        killed = False

        async def read(self, count: int) -> bytes:
            await asyncio.sleep(60)
            return b""

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9

    process = Process()
    process.stdout = process

    async def spawn(*args: Any, **kwargs: Any):
        assert "-passive" in args and "-ni" in args and "-duc" in args
        assert "ZAP_API_KEY" not in kwargs["env"]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(nuclei._run_engine("nuclei", tmp_path, 5))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed
