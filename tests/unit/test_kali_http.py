"""Canonical HTTP receipts over the Kali workroom substrate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import socket
import sys
import threading
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.capability.direct_target import DirectTargetConfig, DirectTargetProviderConfig
from redteam.capability.kali_http import _CLIENT_SOURCE, BuiltinKaliHttpAdapter
from redteam.capability.servers.config import ServerPolicy, SessionProfile, TargetProfile
from redteam.capability.servers.web_http import create_server as create_legacy_web_http_server
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult, ShellWorkroomError, ShellWorkroomManager
from redteam.core.contracts.target import HostTarget, UrlTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation, ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.authentication import http_observation
from redteam.orchestration.observations.web_http import map_web_http_request


class FakeWorkroom:
    def __init__(self, envelope):
        self.policy = SimpleNamespace(max_output_bytes=65536)
        self.envelope = envelope
        self.calls = []
        self.discarded = []

    async def exec_program(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(self.envelope),
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=3,
            workroom_key=args[0],
            container_name="room",
        )

    async def discard_provider_record(self, key, tag):
        self.discarded.append((key, tag))
        return True


class FakeRoute:
    def __init__(self):
        self.calls = []

    async def execute_route(self, invocation, *, target_ref, port, delegate):
        self.calls.append((invocation, target_ref, port))
        result = await delegate()
        result.metadata["direct_target_network"] = "attached_for_execution"
        return result


class MemoryEvidenceStore:
    def __init__(self):
        self.records = []

    async def append(self, evidence):
        await asyncio.sleep(0)
        self.records.append(evidence.model_copy(deep=True))

    async def list_for_run(self, run_id):
        await asyncio.sleep(0)
        return [item.model_copy(deep=True) for item in self.records if item.run_id == run_id]


class AppendThenFailEvidenceStore(MemoryEvidenceStore):
    def __init__(self):
        super().__init__()
        self.failed = False

    async def append(self, evidence):
        await super().append(evidence)
        if (
            not self.failed
            and evidence.metadata.get("record_kind") == "kali_http_ordinal_started"
        ):
            self.failed = True
            raise RuntimeError("append acknowledgement lost")


class RecoverableWorkroom(FakeWorkroom):
    def __init__(self, envelope, state=None, *, hang_first=False, hang_second=False):
        super().__init__(envelope)
        self.state = state or {"records": {}, "meta": {}, "streams": {}, "alive": {}}
        self.hang_first = hang_first
        self.hang_second = hang_second
        self.removed_streams = []
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()

    async def exec_program(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        tag = kwargs["exec_tag"]
        ordinal = int(tag.rsplit("_", 1)[1])
        if (ordinal == 0 and self.hang_first) or (ordinal == 1 and self.hang_second):
            (self.first_started if ordinal == 0 else self.second_started).set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._seal(tag, "cancelled", json.dumps(self.envelope))
                raise
        output = json.dumps(self.envelope)
        self._seal(tag, "completed", output)
        return ExecResult(
            exit_code=0,
            stdout=output,
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=3,
            workroom_key=args[0],
            container_name="room",
        )

    def _seal(self, tag, outcome, stdout):
        raw_out = stdout.encode("utf-8")
        raw_err = b""
        meta = {
            "sealed": True,
            "invocation_id": "inv_http_1",
            "input_digest": self.calls[-1][1]["input_digest"],
            "cap_bytes": self.calls[-1][1]["_provider_output_limit"],
            "started_epoch": 1.0,
            "finished_epoch": 2.0,
            "out": {
                "complete": True,
                "truncated": False,
                "size": len(raw_out),
                "sha256": hashlib.sha256(raw_out).hexdigest(),
            },
            "err": {
                "complete": True,
                "truncated": False,
                "size": 0,
                "sha256": hashlib.sha256(raw_err).hexdigest(),
            },
        }
        canonical = json.dumps(meta, sort_keys=True).encode("utf-8")
        self.state["meta"][tag] = meta
        self.state["streams"][tag] = (raw_out, raw_err)
        self.state["records"][tag] = {
            "outcome": outcome,
            "protected": True,
            "subreaper": True,
            "unreaped": [],
            "spool": "complete",
            "spool_digest": hashlib.sha256(canonical).hexdigest(),
        }
        self.state["alive"][tag] = False

    async def supervisor_record(self, key, tag):
        record = self.state["records"].get(tag)
        return dict(record) if isinstance(record, dict) else None

    async def spool_record(self, key, tag):
        meta = self.state["meta"].get(tag)
        return dict(meta) if isinstance(meta, dict) else None

    async def read_spool_stream(self, key, tag, *, stream="out", limit_bytes=None):
        pair = self.state["streams"].get(tag)
        if pair is None:
            return None
        return pair[0] if stream == "out" else pair[1]

    async def supervisor_outcome(self, key, tag):
        record = self.state["records"].get(tag)
        outcome = record.get("outcome") if isinstance(record, dict) else None
        return outcome, bool(self.state["alive"].get(tag, False))

    async def discard_provider_streams(self, key, tag):
        self.removed_streams.append((key, tag))
        self.state["streams"].pop(tag, None)
        return True

    async def workroom_container_state(self, key):
        return "running"

    async def verify_workroom_ownership(self, key):
        return getattr(self, "ownership_verified", True)


class RecoverableRoute:
    def __init__(self):
        self.active = False
        self.active_count = 0
        self.extension = self

    def inventory_mode(self, _key):
        return "static" if self.active or self.active_count else "full"

    def expected_networks(self, _key):
        if self.active or self.active_count:
            return frozenset({"target-net"})
        return frozenset({"none"})

    async def execute_route(self, invocation, *, target_ref, port, delegate):
        self.active_count += 1
        try:
            result = await delegate()
            result.metadata["direct_target_network"] = "attached_for_execution"
            return result
        finally:
            self.active_count -= 1


class ExclusiveRecoverableRoute(RecoverableRoute):
    async def execute_route(self, invocation, *, target_ref, port, delegate):
        if self.active_count or self.active:
            raise ShellWorkroomError("direct_target_busy")
        return await super().execute_route(
            invocation, target_ref=target_ref, port=port, delegate=delegate
        )


async def test_concurrent_invocations_cannot_enter_the_same_http_ordinal(monkeypatch):
    store = MemoryEvidenceStore()
    workroom = RecoverableWorkroom(envelope(), hang_first=True)
    adapter = BuiltinKaliHttpAdapter(
        workroom,
        RecoverableRoute(),
        provider(),
        policy(monkeypatch),
        evidence_store=store,
    )
    call = invocation()
    first = asyncio.create_task(adapter.invoke_contextual(call))
    await asyncio.wait_for(workroom.first_started.wait(), timeout=1)

    duplicate = await adapter.invoke_contextual(call)

    assert duplicate.success is False
    assert duplicate.error is not None
    assert duplicate.error.code == "kali_http_ordinal_replay_refused"
    assert len(workroom.calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_distinct_adapters_share_route_admission_for_duplicate_invocation(monkeypatch):
    store = MemoryEvidenceStore()
    workroom = RecoverableWorkroom(envelope(), hang_first=True)
    route = ExclusiveRecoverableRoute()
    configured_policy = policy(monkeypatch)
    first_adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), configured_policy, evidence_store=store
    )
    second_adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), configured_policy, evidence_store=store
    )
    call = invocation()
    reference = first_adapter.prepare_reference(call)
    assert reference is not None

    first = asyncio.create_task(first_adapter.invoke_contextual(call))
    await asyncio.wait_for(workroom.first_started.wait(), timeout=1)
    duplicate = await second_adapter.invoke_contextual(call)

    assert duplicate.success is False
    assert duplicate.error is not None
    assert duplicate.error.code == "direct_target_busy"
    assert len(workroom.calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    answer = await second_adapter.query_execution(reference)
    assert answer.execution_terminated is True


def policy(monkeypatch, *, with_session=False):
    sessions = {}
    if with_session:
        monkeypatch.setenv("LAB_TOKEN", "secret-value-never-in-receipt")
        monkeypatch.setenv("LAB_TOKEN_BOB", "second-secret-never-in-receipt")
        sessions["alice"] = SessionProfile(
            target_ref="target_lab",
            principal_ref="principal_alice",
            bearer_env="LAB_TOKEN",
            allowed_paths=["/account"],
        )
        sessions["bob"] = SessionProfile(
            target_ref="target_lab",
            principal_ref="principal_bob",
            bearer_env="LAB_TOKEN_BOB",
            allowed_paths=["/account"],
        )
    return ServerPolicy(
        targets={
            "target_lab": TargetProfile(
                origin="http://lab",
                allowed_paths=["/account"],
                response_body_max_chars=64,
            )
        },
        allowed_networks=["127.0.0.1/32"],
        sessions=sessions,
    )


def provider():
    return DirectTargetProviderConfig(
        network=DirectTargetConfig(targets={"lab": ("127.0.0.1",)}, target_ports={"lab": (80,)}),
        target_route_identity={},
        docker_endpoint={},
    )


def comparison_policy(monkeypatch, *, bob_allowed_paths=None):
    monkeypatch.setenv("LAB_TOKEN", "secret-value-never-in-receipt")
    monkeypatch.setenv("LAB_TOKEN_BOB", "second-secret-never-in-receipt")
    bob = SessionProfile(
        target_ref="target_lab",
        principal_ref="principal_bob",
        bearer_env="LAB_TOKEN_BOB",
        allowed_paths=bob_allowed_paths or ["/account"],
    )
    return ServerPolicy(
        targets={
            "target_lab": TargetProfile(
                origin="http://lab",
                allowed_paths=["/account", "/other"],
                response_body_max_chars=64,
            )
        },
        allowed_networks=["127.0.0.1/32"],
        sessions={
            "alice": SessionProfile(
                target_ref="target_lab",
                principal_ref="principal_alice",
                bearer_env="LAB_TOKEN",
                allowed_paths=["/account"],
            ),
            "bob": bob,
        },
    )


def run_client_source(monkeypatch, wire_response: bytes) -> dict:
    client, peer = socket.socketpair()
    errors = []

    def serve_fixture():
        try:
            peer.settimeout(2)
            request_bytes = bytearray()
            while b"\r\n\r\n" not in request_bytes:
                chunk = peer.recv(4096)
                if not chunk:
                    raise AssertionError("client closed before sending the fixture request")
                request_bytes.extend(chunk)
            peer.sendall(wire_response)
        except BaseException as exc:
            errors.append(exc)
        finally:
            peer.close()

    server = threading.Thread(target=serve_fixture, daemon=True)
    server.start()
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: client)
    request = {
        "origin": "http://lab",
        "scheme": "http",
        "host": "lab",
        "port": 80,
        "connect_ip": "127.0.0.1",
        "method": "GET",
        "path": "/account",
        "request_target": "/account",
        "headers": {"Authorization": "Bearer fixture-secret", "Connection": "close"},
        "body_b64": "",
        "timeout": 1,
        "max_response_bytes": 1024,
        "allowed_paths": ["/account"],
        "allow_all_paths": False,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            try:
                exec(compile(_CLIENT_SOURCE, "<kali-http-client>", "exec"), {})
            except SystemExit:
                pass
    finally:
        client.close()
    server.join(timeout=2)
    assert not server.is_alive()
    assert not errors
    output = json.loads(captured.getvalue())
    assert "fixture-secret" not in json.dumps(output)
    return output


def invocation(*, session_ref=None, tool="http_request"):
    arguments = {"target_ref": "target_lab", "path": "/account"}
    if session_ref is not None:
        arguments["session_ref"] = session_ref
    return ToolInvocation(
        invocation_id=InvocationId("inv_http_1"),
        action_id=ActionId("act_http_1"),
        run_id=RunId("run_http_1"),
        tool_id=ToolId(f"web.web-http.{tool}"),
        arguments=arguments,
        timeout_seconds=10,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_http_1"),
            run_id=RunId("run_http_1"),
            instance_id=AgentInstanceId("inst_http_1"),
            approved_target=HostTarget(host="lab"),
        ),
        created_at=datetime.now(timezone.utc),
    )


def envelope(body=b"ok", *, status=200, redirect=None):
    safe = {
        "names": ["content-type", "x-content-type-options"],
        "content_type": "text/plain",
        "security_headers_present": {
            "strict-transport-security": False,
            "content-security-policy": False,
            "x-content-type-options": True,
            "cache-control": False,
        },
        "posture_headers_present": {},
        "cookies": [],
        "set_cookie_header_count": 0,
        "values_omitted": True,
    }
    if redirect is not None:
        safe["redirect"] = redirect
    return {
        "ok": True,
        "status": status,
        "body_b64": base64.b64encode(body).decode(),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body),
        "body_complete": True,
        "body_truncated": False,
        "safe_headers": safe,
        "http_version": 11,
    }


async def test_provider_program_envelope_has_separate_bounded_limits():
    manager = ShellWorkroomManager(
        ShellWorkroomPolicy(image="fixture", allow_unpinned_image=True, max_script_bytes=1024)
    )
    result = ExecResult(
        exit_code=0,
        stdout="{}",
        stderr="",
        timed_out=False,
        truncated=False,
        duration_ms=1,
        workroom_key="run.instance",
        container_name="room",
    )
    manager._exec_supervised = AsyncMock(return_value=result)

    received = await manager.exec_program(
        "run.instance",
        "/usr/bin/python3",
        ["-c", _CLIENT_SOURCE],
        "{}",
        invocation_id="inv_http_1",
        input_digest="a" * 64,
        _provider_input_limit=256 * 1024,
        _provider_output_limit=256 * 1024,
        _provider_argv_limit=64 * 1024,
    )

    assert received is result
    assert manager._exec_supervised.await_args.kwargs["output_limit"] == 256 * 1024


async def test_provider_stream_cleanup_retains_terminal_seal_and_metadata():
    manager = ShellWorkroomManager(
        ShellWorkroomPolicy(image="fixture", allow_unpinned_image=True)
    )
    manager._known["run.instance"] = "room"
    manager._run_docker = AsyncMock(return_value=(0, b"", b"", False, False))

    assert await manager.discard_provider_streams("run.instance", "khttp_fixture_0")

    argv = manager._run_docker.await_args.args[0]
    assert argv[-4:] == (
        "/bin/rm", "-f",
        "/run/whs-shell/.whs-spool-khttp_fixture_0.out",
        "/run/whs-shell/.whs-spool-khttp_fixture_0.err",
    )
    assert not any(".whs-result-" in item or ".whs-pgid-" in item for item in argv)
    assert not any(".whs-spool-khttp_fixture_0.meta" in item for item in argv)


async def test_public_identity_is_preserved_and_only_registered_provider_emits_receipts(
    monkeypatch,
):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    descriptors = await adapter.discover_tools()

    assert {str(item.tool_id) for item in descriptors} == {
        "web.web-http.session_list",
        "web.web-http.http_request",
        "web.web-http.http_compare_sessions",
        "web.web-http.http_security_posture",
        "web.web-http.http_compare_anonymous",
    }
    request = next(item for item in descriptors if item.tool_name == "http_request")
    session_list = next(item for item in descriptors if item.tool_name == "session_list")
    assert request.execution_route == "target"
    assert session_list.execution_route is None
    assert set(request.input_schema["properties"]) == {
        "target_ref",
        "path",
        "method",
        "session_ref",
        "json_body",
        "form_body",
        "query",
        "request_headers",
    }
    legacy = {
        item.name: item
        for item in await create_legacy_web_http_server(policy(monkeypatch)).list_tools()
    }
    for item in descriptors:
        assert item.input_schema == legacy[item.tool_name].input_schema
        assert item.output_schema == legacy[item.tool_name].output_schema
    path_description = request.input_schema["properties"]["path"]["description"]
    query_description = request.input_schema["properties"]["query"]["description"]
    assert "exact-path allowlists require an exact configured path" in path_description
    assert "whole-origin paths must be explicitly enabled" in path_description
    assert "separately in query" in path_description
    assert "configured per-path or global query policy" in query_description
    assert "explicit allow-all setting" in query_description
    assert "do not encode them into path" in query_description


async def test_session_inventory_effect_metadata_reaches_cleanup_without_clearing_unknowns(
    monkeypatch,
):
    from mcp import Client

    from redteam.capability import kali_http
    from redteam.capability.adapters.mcp.transport import remote_tool_metadata
    from redteam.core.contracts.tool import PROVIDER_METADATA_RESOURCE_EFFECTS
    from redteam.orchestration.resource_cleanup import unmapped_resource_effects

    public_server = create_legacy_web_http_server(policy(monkeypatch))
    async with Client(public_server) as client:
        public_tools = (await client.list_tools()).tools
    remote_session_list = next(tool for tool in public_tools if tool.name == "session_list")
    assert remote_tool_metadata(remote_session_list)[PROVIDER_METADATA_RESOURCE_EFFECTS] == []

    class PublicSDKDefinitions:
        async def list_tools(self):
            return public_tools

    monkeypatch.setattr(
        kali_http,
        "create_legacy_web_http_server",
        lambda _policy: PublicSDKDefinitions(),
    )
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    inventory_result = await adapter.invoke_contextual(
        invocation(tool="session_list").model_copy(update={"arguments": {}})
    )
    assert inventory_result.metadata["resource_effects"] == []
    assert workroom.calls == []
    assert route.calls == []

    descriptors = await adapter.discover_tools()
    by_name = {item.tool_name: item for item in descriptors}

    assert by_name["session_list"].resource_effects == []
    assert all(
        by_name[name].resource_effects is None
        for name in (
            "http_request",
            "http_compare_sessions",
            "http_security_posture",
            "http_compare_anonymous",
        )
    )
    request_result = await adapter.invoke_contextual(invocation())
    posture_result = await adapter.invoke_contextual(invocation(tool="http_security_posture"))
    assert "resource_effects" not in request_result.metadata
    assert "resource_effects" not in posture_result.metadata

    original_metadata = {
        "inventory": inventory_result.metadata,
        "shell": {},
        "recon": {},
        "request": request_result.metadata,
        "posture": posture_result.metadata,
    }

    class EvidenceStore:
        async def get(self, evidence_id):
            return SimpleNamespace(
                metadata={
                    "original_result": {"metadata": original_metadata[evidence_id]}
                }
            )

    executions = [
        SimpleNamespace(tool_id=tool_id, result_evidence_id=evidence_id)
        for evidence_id, tool_id in (
            ("inventory", "web.web-http.session_list"),
            ("shell", "analysis.shell_target_run"),
            ("recon", "recon.recon-passive.http_probe"),
            ("request", "web.web-http.http_request"),
            ("posture", "web.web-http.http_security_posture"),
        )
    ]

    assert await unmapped_resource_effects(EvidenceStore(), executions) == [
        "analysis.shell_target_run",
        "recon.recon-passive.http_probe",
        "web.web-http.http_request",
        "web.web-http.http_security_posture",
    ]


@pytest.mark.parametrize("path", ["/account?user=alice", "/account#section"])
async def test_query_or_fragment_in_path_is_rejected_before_route_or_workroom(monkeypatch, path):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    call = invocation().model_copy(update={"arguments": {"target_ref": "target_lab", "path": path}})

    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error is not None
    assert result.error.code == "path outside configured allowlist"
    assert route.calls == []
    assert workroom.calls == []


async def test_configured_query_is_passed_separately_from_literal_path(monkeypatch):
    configured_policy = ServerPolicy(
        targets={
            "target_lab": TargetProfile(
                origin="http://lab",
                allowed_paths=["/account"],
                allowed_query_keys={"/account": ["user"]},
                response_body_max_chars=64,
            )
        },
        allowed_networks=["127.0.0.1/32"],
    )
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), configured_policy)
    call = invocation().model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "query": {"user": "alice"},
            }
        }
    )

    result = await adapter.invoke_contextual(call)

    assert result.success
    assert route.calls[0][1:] == ("lab", 80)
    request = json.loads(workroom.calls[0][0][3])
    assert request["path"] == "/account"
    assert request["request_target"] == "/account?user=alice"


async def test_anonymous_receipt_is_bound_to_kali_route_and_consumed(monkeypatch):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    call = invocation()
    result = await adapter.invoke_contextual(call)

    assert result.success
    assert route.calls[0][1:] == ("lab", 80)
    assert 4096 < len(_CLIENT_SOURCE.encode()) <= workroom.calls[0][1]["_provider_argv_limit"]
    assert workroom.discarded == [
        ("run_http_1.inst_http_1", workroom.calls[0][1]["exec_tag"])
    ]
    output = result.output
    receipt = output["http_receipt"]
    assert output["actual_session_ref"] is None
    assert output["principal_scope"] == "anonymous"
    assert receipt["request"]["principal_scope"] == "anonymous"
    assert receipt["response"]["request_binding"] == receipt["request"]["binding"]
    assert receipt["response"]["peer"] == {"ip": "127.0.0.1", "port": 80}
    assert receipt["response"]["body"]["retention"] == "digest_only"
    assert "body_b64" not in json.dumps(output)

    public = ToolResult(
        invocation_id=call.invocation_id,
        action_id=call.action_id,
        tool_id=call.tool_id,
        success=True,
        output=output,
        started_at=result.started_at,
        finished_at=result.finished_at,
        metadata=result.metadata,
    )
    observation = http_observation(call.arguments, public)
    assert observation["receipt_version"] == 1
    assert observation["principal_scope"] == "anonymous"
    assert observation["request_binding"] == receipt["request"]["binding"]
    assert map_web_http_request(
        target=call.execution_context.approved_target,
        arguments=call.arguments,
        result=public,
    ).nodes


async def test_cancelled_session_comparison_keeps_safe_ordinals_and_restart_termination_proof(
    monkeypatch,
):
    store = MemoryEvidenceStore()
    workroom = RecoverableWorkroom(
        envelope(b"private-response-fragment"), hang_second=True
    )
    route = RecoverableRoute()
    configured_policy = comparison_policy(monkeypatch)
    adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), configured_policy, evidence_store=store
    )
    call = invocation(tool="http_compare_sessions").model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            }
        }
    )
    reference = adapter.prepare_reference(call)
    assert reference is not None
    first_call = asyncio.create_task(adapter.invoke_contextual(call))
    gateway_wait = asyncio.create_task(asyncio.wait_for(first_call, timeout=10))
    await asyncio.wait_for(workroom.second_started.wait(), timeout=1)
    gateway_wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gateway_wait

    assert len(workroom.calls) == 2
    assert workroom.calls[0][1]["exec_tag"] != workroom.calls[1][1]["exec_tag"]
    provider_records = [
        record for record in store.records
        if record.source_type is EvidenceSourceType.SYSTEM
        and record.source_id == "capability.kali_http"
    ]
    assert len(provider_records) == 4
    serialized = json.dumps([record.metadata for record in provider_records], sort_keys=True)
    assert "private-response-fragment" not in serialized
    assert "secret-value-never-in-receipt" not in serialized
    assert "second-secret-never-in-receipt" not in serialized
    assert "body_b64" not in serialized
    ordinal_receipts = [
        record.metadata["safe_result"] for record in provider_records
        if record.metadata["record_kind"] == "kali_http_ordinal_receipt"
    ]
    assert [item["ordinal"] for item in ordinal_receipts] == [0, 1]
    assert all(
        item["http_receipt"]["response"]["body"]["retention"] == "digest_only"
        for item in ordinal_receipts
    )
    assert all("body_preview" not in item for item in ordinal_receipts)
    assert all(
        tag not in workroom.state["streams"]
        for tag in reference.provider_handle["ordinal_tags"]
    )

    # A fresh adapter/route sees only trusted provider records and detached
    # workroom state. It can release capacity while keeping the result UNKNOWN.
    restarted = BuiltinKaliHttpAdapter(
        RecoverableWorkroom(envelope(), workroom.state),
        RecoverableRoute(),
        provider(),
        configured_policy,
        evidence_store=store,
    )
    answer = await restarted.query_execution(reference)
    assert answer.state.value == "unknown"
    assert answer.verified is True
    assert answer.uncertain is True
    assert answer.result_available is False
    assert answer.execution_terminated is True
    assert await restarted.fetch_execution(reference) is None

    # A duplicate invocation with the same identity is held before exec_program.
    replay = await restarted.invoke_contextual(call)
    assert replay.success is False
    assert replay.error is not None
    assert replay.error.code == "kali_http_ordinal_replay_refused"
    assert len(restarted._workroom.calls) == 0

    # Either an attached route or an untrusted terminal seal defeats the
    # independent termination proof.
    restarted._executor.active = True
    attached = await restarted.query_execution(reference)
    assert attached.execution_terminated is False
    restarted._executor.active = False
    first_tag = reference.provider_handle["ordinal_tags"][0]
    restarted._workroom.state["records"][first_tag]["unreaped"] = [1234]
    untrusted = await restarted.query_execution(reference)
    assert untrusted.state.value == "unknown"
    assert untrusted.execution_terminated is False


@pytest.mark.parametrize(
    "failure",
    [
        "missing_marker",
        "corrupt_marker_digest",
        "duplicate_marker",
        "supervisor_alive",
        "supervisor_error",
        "route_unowned",
    ],
)
async def test_termination_query_fails_closed_on_ambiguous_provider_state(monkeypatch, failure):
    store = MemoryEvidenceStore()
    original_workroom = RecoverableWorkroom(envelope())
    route = RecoverableRoute()
    adapter = BuiltinKaliHttpAdapter(
        original_workroom, route, provider(), policy(monkeypatch), evidence_store=store
    )
    call = invocation()
    reference = adapter.prepare_reference(call)
    assert reference is not None
    result = await adapter.invoke_contextual(call)
    assert result.success

    restarted_workroom = RecoverableWorkroom(envelope(), original_workroom.state)
    if failure == "missing_marker":
        store.records = [
            item for item in store.records
            if item.metadata.get("record_kind") != "kali_http_ordinal_started"
        ]
    elif failure == "corrupt_marker_digest":
        marker = next(
            item for item in store.records
            if item.metadata.get("record_kind") == "kali_http_ordinal_started"
        )
        marker.digest = "0" * 64
    elif failure == "duplicate_marker":
        marker = next(
            item for item in store.records
            if item.metadata.get("record_kind") == "kali_http_ordinal_started"
        )
        store.records.append(
            marker.model_copy(update={"evidence_id": EvidenceId("evidence_duplicate")})
        )
    elif failure == "supervisor_alive":
        tag = reference.provider_handle["ordinal_tags"][0]
        restarted_workroom.state["alive"][tag] = True
    elif failure == "supervisor_error":
        restarted_workroom.supervisor_outcome = AsyncMock(side_effect=RuntimeError("unreadable"))
    elif failure == "route_unowned":
        restarted_workroom.ownership_verified = False

    restarted = BuiltinKaliHttpAdapter(
        restarted_workroom, route, provider(), policy(monkeypatch), evidence_store=store
    )

    answer = await restarted.query_execution(reference)

    assert answer.state.value == "unknown"
    assert answer.verified is False
    assert answer.execution_terminated is False


async def test_durable_but_unacknowledged_start_marker_blocks_replay(monkeypatch):
    store = AppendThenFailEvidenceStore()
    workroom = RecoverableWorkroom(envelope())
    adapter = BuiltinKaliHttpAdapter(
        workroom, RecoverableRoute(), provider(), policy(monkeypatch), evidence_store=store
    )
    call = invocation()

    first = await adapter.invoke_contextual(call)
    replay = await adapter.invoke_contextual(call)

    assert first.success is False
    assert replay.success is False
    assert replay.error is not None
    assert replay.error.code == "kali_http_ordinal_replay_refused"
    assert workroom.calls == []
    assert len(store.records) == 1


async def test_cancelled_spool_salvage_uses_provider_output_cap(monkeypatch):
    store = MemoryEvidenceStore()
    workroom = RecoverableWorkroom(envelope(b"x" * 100_000), hang_first=True)
    adapter = BuiltinKaliHttpAdapter(
        workroom, RecoverableRoute(), provider(), policy(monkeypatch), evidence_store=store
    )
    call = invocation()
    reference = adapter.prepare_reference(call)
    assert reference is not None
    task = asyncio.create_task(adapter.invoke_contextual(call))
    await asyncio.wait_for(workroom.first_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    receipts = [
        item.metadata["safe_result"] for item in store.records
        if item.metadata.get("record_kind") == "kali_http_ordinal_receipt"
    ]
    assert len(receipts) == 1
    assert receipts[0]["http_receipt"]["response"]["body"]["size_bytes"] == 100_000


async def test_single_http_request_receives_recovery_reference(monkeypatch):
    adapter = BuiltinKaliHttpAdapter(
        FakeWorkroom(envelope()),
        FakeRoute(),
        provider(),
        policy(monkeypatch),
        evidence_store=MemoryEvidenceStore(),
    )

    reference = adapter.prepare_reference(invocation())

    assert reference is not None
    assert len(reference.provider_handle["ordinal_tags"]) == 1


async def test_url_target_scheme_mismatch_is_rejected_before_route_open(monkeypatch):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    call = invocation()
    approved = call.execution_context.model_copy(
        update={"approved_target": UrlTarget(url="https://lab:80/account")}
    )
    call = call.model_copy(update={"execution_context": approved})

    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error is not None
    assert result.error.code == "kali_http_url_scheme_mismatch"
    assert route.calls == []
    assert workroom.calls == []


async def test_url_target_matching_scheme_keeps_approved_route(monkeypatch):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    call = invocation()
    approved = call.execution_context.model_copy(
        update={"approved_target": UrlTarget(url="http://lab:80/account")}
    )
    call = call.model_copy(update={"execution_context": approved})

    result = await adapter.invoke_contextual(call)

    assert result.success
    assert route.calls[0][1:] == ("lab", 80)


async def test_session_secret_is_stdin_only_and_never_returned(monkeypatch):
    workroom, route = FakeWorkroom(envelope(body=b"private")), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), policy(monkeypatch, with_session=True)
    )
    result = await adapter.invoke_contextual(invocation(session_ref="alice"))

    assert result.success
    stdin_envelope = json.loads(workroom.calls[0][0][3])
    assert stdin_envelope["headers"]["Authorization"].endswith("secret-value-never-in-receipt")
    serialized = json.dumps(result.output)
    assert "secret-value-never-in-receipt" not in serialized
    assert result.output["actual_session_ref"] == "alice"
    assert result.output["principal_ref"] == "principal_alice"
    assert result.output["http_receipt"]["request"]["principal_scope"] == "session"


async def test_missing_session_and_out_of_scope_redirect_never_succeed(monkeypatch):
    missing_workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(missing_workroom, route, provider(), policy(monkeypatch))
    missing = await adapter.invoke_contextual(invocation(session_ref="missing"))
    assert not missing.success
    assert missing_workroom.calls == []

    redirect = {
        "detected": True,
        "decision": "not_followed",
        "same_origin": False,
        "in_scope": False,
        "location_path": "/elsewhere",
        "query_omitted": False,
        "fragment_omitted": False,
    }
    redirect_workroom = FakeWorkroom(envelope(status=302, redirect=redirect))
    adapter = BuiltinKaliHttpAdapter(
        redirect_workroom, FakeRoute(), provider(), policy(monkeypatch)
    )
    denied = await adapter.invoke_contextual(invocation())
    assert not denied.success
    assert denied.error.code == "target_http_redirect_scope_denied"


async def test_session_comparison_uses_two_independent_request_bindings(monkeypatch):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), policy(monkeypatch, with_session=True)
    )
    call = invocation(tool="http_compare_sessions").model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            }
        }
    )
    result = await adapter.invoke_contextual(call)

    assert result.success and result.output["request_count"] == 2
    first, second = result.output["first"], result.output["second"]
    assert first["actual_session_ref"] == "alice"
    assert second["actual_session_ref"] == "bob"
    assert (
        first["http_receipt"]["request"]["binding"] != second["http_receipt"]["request"]["binding"]
    )
    assert len(route.calls) == 2


@pytest.mark.parametrize(
    ("wire_response", "expected_ok", "expected_code", "expected_body"),
    [
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc",
            True,
            None,
            b"abc",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nabc",
            False,
            "target_http_incomplete_response",
            b"abc",
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"3\r\nabc\r\n0\r\n\r\n",
            True,
            None,
            b"abc",
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\nabc",
            False,
            "target_http_incomplete_response",
            b"abc",
        ),
    ],
    ids=["complete-content-length", "short-content-length", "complete-chunked", "short-chunked"],
)
def test_client_source_requires_complete_http_framing(
    monkeypatch, wire_response, expected_ok, expected_code, expected_body
):
    raw = run_client_source(monkeypatch, wire_response)

    assert raw["ok"] is expected_ok
    assert base64.b64decode(raw["body_b64"]) == expected_body
    assert raw["body_bytes"] == len(expected_body)
    assert raw["body_sha256"] == hashlib.sha256(expected_body).hexdigest()
    assert raw["body_complete"] is expected_ok
    if expected_ok:
        assert "diagnostic" not in raw
    else:
        assert raw["diagnostic"] == {
            "code": expected_code,
            "phase": "response",
            "response_received": True,
            "partial_response": True,
        }
        assert raw["body_truncated"] is False


@pytest.mark.parametrize("failure", ["credential", "session_path"])
async def test_session_comparison_preflights_both_sessions_before_route(
    monkeypatch, failure
):
    if failure == "session_path":
        configured_policy = comparison_policy(monkeypatch, bob_allowed_paths=["/other"])
    else:
        configured_policy = comparison_policy(monkeypatch)
        monkeypatch.delenv("LAB_TOKEN_BOB")
    expected_code = {
        "credential": "session credential unavailable",
        "session_path": "path outside configured session allowlist",
    }[failure]
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), configured_policy)
    call = invocation(tool="http_compare_sessions").model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            }
        }
    )

    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error is not None
    assert result.error.code == expected_code
    assert route.calls == []
    assert workroom.calls == []
    assert workroom.discarded == []


async def test_session_comparison_preserves_first_receipt_if_second_transport_fails(monkeypatch):
    failure_body = b"second-partial"
    second_failure = envelope(failure_body)
    second_failure.update(
        ok=False,
        diagnostic={
            "code": "target_http_response_timeout",
            "phase": "response",
            "response_received": True,
            "partial_response": True,
        },
        body_complete=False,
    )
    workroom, route = FakeWorkroom(envelope(b"first-complete")), FakeRoute()
    responses = [envelope(b"first-complete"), second_failure]

    async def exec_program(*args, **kwargs):
        workroom.calls.append((args, kwargs))
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(responses.pop(0)),
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=3,
            workroom_key=args[0],
            container_name="room",
        )

    workroom.exec_program = exec_program
    adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), comparison_policy(monkeypatch)
    )
    call = invocation(tool="http_compare_sessions").model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            }
        }
    )
    first_receipts = []
    original_one = adapter._one

    async def capture_first_receipt(*args, **kwargs):
        result = await original_one(*args, **kwargs)
        if kwargs.get("ordinal") == 0 and result.success:
            first_receipts.append(result.output["http_receipt"])
        return result

    adapter._one = capture_first_receipt
    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error is not None
    assert result.error.code == "target_http_response_timeout"
    assert result.metadata["execution_started"] is True
    partial = result.output["partial_comparison"]
    assert partial["completed_request_count"] == 1
    assert partial["failed_request_ordinal"] == 1
    assert partial["first"]["actual_session_ref"] == "alice"
    assert partial["first"]["principal_ref"] == "principal_alice"
    assert partial["first"]["http_receipt"] == first_receipts[0]
    assert partial["second"]["http_receipt"]["response"]["failure_phase"] == "response"
    assert result.output["diagnostic"] == {
        "code": "kali_http_partial_session_comparison",
        "phase": "second_request",
        "failed_request_code": "target_http_response_timeout",
    }
    assert len(route.calls) == 2
    assert len(workroom.calls) == 2
    assert "secret-value-never-in-receipt" not in json.dumps(result.output)
    assert "second-secret-never-in-receipt" not in json.dumps(result.output)


@pytest.mark.parametrize(
    ("failure_mode", "expected_code"),
    [
        ("cleanup", "kali_http_spool_cleanup_failed"),
        ("interrupted", "shell_execution_interrupted"),
    ],
)
async def test_session_comparison_preserves_first_receipt_on_uncertain_second_exception(
    monkeypatch, failure_mode, expected_code
):
    workroom, route = FakeWorkroom(envelope(b"first-complete")), FakeRoute()
    execution_count = 0

    async def exec_program(*args, **kwargs):
        nonlocal execution_count
        execution_count += 1
        workroom.calls.append((args, kwargs))
        if failure_mode == "interrupted" and execution_count == 2:
            raise ShellWorkroomError("shell_execution_interrupted", uncertainty=True)
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(envelope(b"first-complete")),
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=3,
            workroom_key=args[0],
            container_name="room",
        )

    workroom.exec_program = exec_program
    if failure_mode == "cleanup":
        async def cleanup_second_fails(key, tag):
            workroom.discarded.append((key, tag))
            return len(workroom.discarded) < 2

        workroom.discard_provider_record = cleanup_second_fails

    adapter = BuiltinKaliHttpAdapter(
        workroom, route, provider(), comparison_policy(monkeypatch)
    )
    call = invocation(tool="http_compare_sessions").model_copy(
        update={
            "arguments": {
                "target_ref": "target_lab",
                "path": "/account",
                "first_session_ref": "alice",
                "second_session_ref": "bob",
            }
        }
    )
    first_receipts = []
    original_one = adapter._one

    async def capture_first_receipt(*args, **kwargs):
        result = await original_one(*args, **kwargs)
        if kwargs.get("ordinal") == 0 and result.success:
            first_receipts.append(result.output["http_receipt"])
        return result

    adapter._one = capture_first_receipt
    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error is not None
    assert result.error.code == expected_code
    assert result.error.retryable is False
    assert result.error.message == "Kali HTTP workroom rejected the request"
    assert result.metadata["execution_uncertain"] is True
    assert result.metadata["execution_started"] is True
    assert result.metadata["partial_comparison"] is True
    partial = result.output["partial_comparison"]
    assert partial["outcome"] == "unknown"
    assert partial["first"]["actual_session_ref"] == "alice"
    assert partial["first"]["principal_ref"] == "principal_alice"
    assert partial["first"]["http_receipt"] == first_receipts[0]
    assert partial["second"]["diagnostic"] == {
        "code": expected_code,
        "phase": "execution",
    }
    assert partial["second"]["http_receipt"] is None
    assert result.output["diagnostic"]["outcome"] == "unknown"
    assert len(route.calls) == 2
    assert execution_count == 2
    assert len(workroom.discarded) == 2
    assert "secret-value-never-in-receipt" not in json.dumps(result.output)
    assert "second-secret-never-in-receipt" not in json.dumps(result.output)


async def test_unverified_protocol_spool_cleanup_fails_loudly(monkeypatch):
    workroom = FakeWorkroom(envelope())

    async def cleanup_failed(key, tag):
        workroom.discarded.append((key, tag))
        return False

    workroom.discard_provider_record = cleanup_failed
    adapter = BuiltinKaliHttpAdapter(workroom, FakeRoute(), provider(), policy(monkeypatch))
    result = await adapter.invoke_contextual(invocation())

    assert not result.success
    assert result.error.code == "kali_http_spool_cleanup_failed"
    assert result.metadata["execution_uncertain"] is True


async def test_partial_response_is_structured_failure_and_not_mapped(monkeypatch):
    body = b"partial"
    raw = envelope(body)
    raw.update(
        ok=False,
        diagnostic={
            "code": "target_http_response_timeout",
            "phase": "response",
            "response_received": True,
            "partial_response": True,
        },
        body_complete=False,
    )
    workroom = FakeWorkroom(raw)
    call = invocation()
    adapter = BuiltinKaliHttpAdapter(workroom, FakeRoute(), provider(), policy(monkeypatch))
    result = await adapter.invoke_contextual(call)

    assert not result.success
    assert result.error.code == "target_http_response_timeout"
    assert result.output["http_receipt"]["response"]["failure_phase"] == "response"
    public = ToolResult(
        invocation_id=call.invocation_id,
        action_id=call.action_id,
        tool_id=call.tool_id,
        success=False,
        output=result.output,
        error=result.error,
        started_at=result.started_at,
        finished_at=result.finished_at,
        metadata=result.metadata,
    )
    assert http_observation(call.arguments, public) is None
    assert not map_web_http_request(
        target=call.execution_context.approved_target,
        arguments=call.arguments,
        result=public,
    ).nodes


async def test_declared_but_tampered_receipt_cannot_fall_back_to_legacy_summary(monkeypatch):
    workroom = FakeWorkroom(envelope())
    call = invocation()
    adapter = BuiltinKaliHttpAdapter(workroom, FakeRoute(), provider(), policy(monkeypatch))
    result = await adapter.invoke_contextual(call)
    result.output["http_receipt"]["request"]["invocation_id"] = "inv_other"
    public = ToolResult(
        invocation_id=call.invocation_id,
        action_id=call.action_id,
        tool_id=call.tool_id,
        success=True,
        output=result.output,
        started_at=result.started_at,
        finished_at=result.finished_at,
        metadata=result.metadata,
    )

    assert http_observation(call.arguments, public) is None
    assert not map_web_http_request(
        target=call.execution_context.approved_target,
        arguments=call.arguments,
        result=public,
    ).nodes
