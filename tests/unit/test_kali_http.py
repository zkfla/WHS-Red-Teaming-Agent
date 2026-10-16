"""Canonical HTTP receipts over the Kali workroom substrate."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redteam.capability.direct_target import DirectTargetConfig, DirectTargetProviderConfig
from redteam.capability.kali_http import _CLIENT_SOURCE, BuiltinKaliHttpAdapter
from redteam.capability.servers.config import ServerPolicy, SessionProfile, TargetProfile
from redteam.capability.servers.web_http import create_server as create_legacy_web_http_server
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult, ShellWorkroomManager
from redteam.core.contracts.target import HostTarget
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
