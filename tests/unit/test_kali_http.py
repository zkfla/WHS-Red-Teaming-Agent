"""Canonical HTTP receipts over the Kali workroom substrate."""

from __future__ import annotations

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
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
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


async def test_anonymous_receipt_is_bound_to_kali_route_and_consumed(monkeypatch):
    workroom, route = FakeWorkroom(envelope()), FakeRoute()
    adapter = BuiltinKaliHttpAdapter(workroom, route, provider(), policy(monkeypatch))
    call = invocation()
    result = await adapter.invoke_contextual(call)

    assert result.success
    assert route.calls[0][1:] == ("lab", 80)
    assert 4096 < len(_CLIENT_SOURCE.encode()) <= workroom.calls[0][1]["_provider_argv_limit"]
    assert workroom.discarded == [("run_http_1.inst_http_1", "inv_http_1")]
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
