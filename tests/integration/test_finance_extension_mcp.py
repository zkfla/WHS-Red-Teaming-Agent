"""New MCP endpoints run through the actual MCP client with bounded engine fixtures."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from redteam.capability.servers import nuclei, zap
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ResolvedTarget


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
        return CapturedResponse(
            200,
            {"set-cookie": "secret", "content-length": "99"},
            b"secret",
            actual_session_ref="bank-session",
            has_session_receipt=True,
        )

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
    assert result.structured_content["actual_session_ref"] == "bank-session"
    assert set(result.structured_content) == {
        "target_ref", "check_id", "matched", "status_code", "request_count",
        "engine_mode", "raw_output_omitted", "verdict", "actual_session_ref",
    }
    assert "must-be-omitted" not in str(result.structured_content)
    assert "secret" not in str(result.structured_content)
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


async def test_nuclei_active_probe_uses_allowlisted_path_query_and_pinned_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_policy = ServerPolicy.model_validate({
        "targets": {
            "bank": {
                "origin": "https://bank.test:8443",
                "allowed_paths": ["/search"],
                "allowed_query_keys": {"/search": ["term"]},
            }
        },
        "allowed_networks": ["192.0.2.0/24"],
        "active_enabled": True,
    })
    profile = active_policy.targets["bank"]
    calls: list[dict[str, Any]] = []

    async def resolve(_policy: ServerPolicy, target_ref: str) -> ResolvedTarget:
        assert target_ref == "bank"
        return ResolvedTarget(profile, "bank.test", 8443, ("192.0.2.10",))

    async def engine(
        binary: str,
        directory: Path,
        **kwargs: Any,
    ) -> bytes:
        calls.append(kwargs)
        template = yaml.safe_load((directory / "active-check.yaml").read_text())
        assert template["http"][0]["path"] == [
            "{{BaseURL}}?term=redteam_capability_reflection_probe_7f3a"
        ]
        assert binary == "/test/nuclei"
        return b'{"template-id":"bounded-reflected-query-marker"}\n'

    monkeypatch.setattr(nuclei.shutil, "which", lambda _: "/test/nuclei")
    monkeypatch.setattr(nuclei, "resolve_target", resolve)
    monkeypatch.setattr(nuclei, "_run_active_engine", engine)
    async with Client(nuclei.create_server(active_policy)) as client:
        result = await client.call_tool("nuclei_active_query_probe", {
            "target_ref": "bank",
            "path": "/search",
            "query_key": "term",
        })

    assert result.structured_content is not None
    assert result.structured_content["matched"] is True
    assert result.structured_content["request_count"] == 1
    assert result.structured_content["verdict"] == "observation_only"
    assert calls == [{
        "target_url": "https://192.0.2.10:8443/search",
        "host_header": "bank.test:8443",
        "sni_hostname": "bank.test",
        "timeout": 15.0,
    }]


async def test_nuclei_active_engine_disables_redirect_retry_oast_and_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Process:
        returncode: int | None = None
        stdout: Any

        def __init__(self) -> None:
            self.chunks = [b'{"template-id":"bounded-reflected-query-marker"}\n', b""]

        async def read(self, count: int) -> bytes:
            return self.chunks.pop(0)

        async def wait(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    process = Process()
    process.stdout = process
    captured: dict[str, Any] = {}

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    output = await nuclei._run_active_engine(
        "nuclei",
        tmp_path,
        target_url="https://192.0.2.10:8443/search",
        host_header="bank.test:8443",
        sni_hostname="bank.test",
        timeout=1,
    )

    args = captured["args"]
    assert output.startswith(b'{"template-id"')
    assert ("-u", "https://192.0.2.10:8443/search") == args[1:3]
    assert "Host: bank.test:8443" in args
    assert all(flag in args for flag in ("-dr", "-ni", "-duc", "-retries", "-sni"))
    assert args[args.index("-retries") + 1] == "0"
    assert args[args.index("-rate-limit") + 1] == "1"
    assert captured["env"]["DISABLE_NUCLEI_TEMPLATES_PUBLIC_DOWNLOAD"] == "true"


@pytest.mark.parametrize(
    "arguments",
    [
        {"target_ref": "bank", "path": "/", "query_key": "probe"},
        {
            "target_ref": "bank",
            "path": "/",
            "query_key": "probe",
            "check_id": "community-template",
        },
    ],
)
async def test_nuclei_active_probe_fails_before_engine_when_disabled_or_unreviewed(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, str],
) -> None:
    called = False

    async def engine(*args: Any, **kwargs: Any) -> bytes:
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr(nuclei.shutil, "which", lambda _: "/test/nuclei")
    monkeypatch.setattr(nuclei, "_run_active_engine", engine)
    async with Client(nuclei.create_server(policy())) as client:
        result = await client.call_tool("nuclei_active_query_probe", arguments)

    assert result.is_error
    assert called is False
