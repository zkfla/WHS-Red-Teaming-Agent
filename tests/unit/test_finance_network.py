"""Target pinning, auth isolation, bounded output and absence of automatic replay."""

import asyncio
import socket
from typing import Any

import httpx
import pytest

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError, request, resolve_target


class _CleanupTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler, close=None) -> None:
        self.handler = handler
        self.close = close
        self.close_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.handler(request)

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close is not None:
            await self.close()


class _FailingBodyStream(httpx.AsyncByteStream):
    def __init__(self, *, close_fails: bool = False) -> None:
        self.close_fails = close_fails
        self.close_calls = 0

    async def __aiter__(self):
        raise httpx.ReadTimeout("body timed out")
        yield b""  # Keep this an async generator.

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_fails:
            raise OSError("response stream close failed")


@pytest.mark.parametrize("failure,code,phase", [
    (httpx.ConnectTimeout("secret connection information"),
     "target_http_connect_timeout", "connect"),
    (httpx.ConnectError("secret connection information"), "target_http_connect_failed", "connect"),
    (httpx.ReadTimeout("secret connection information"),
     "target_http_response_timeout", "request_or_response"),
])
async def test_network_failure_preserves_safe_stage_and_no_retry(
    policy, monkeypatch, failure, code, phase,
):
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])
    calls = []

    def fail(req):
        calls.append(req)
        raise failure

    with pytest.raises(TargetRequestError) as captured:
        await request(policy, "bank", "/account", transport=httpx.MockTransport(fail))
    diagnostic = captured.value.diagnostic
    assert diagnostic["code"] == code and diagnostic["phase"] == phase
    assert diagnostic["response_received"] is (False if phase == "connect" else None)
    assert diagnostic["http_request_attempted"] is (phase == "request_or_response")
    assert diagnostic["execution_uncertain"] is (phase == "request_or_response")
    assert diagnostic["repair_condition"]
    assert "secret" not in str(captured.value) and len(calls) == 1


async def test_preheader_timeout_has_no_termination_proof_from_client_close_alone(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])

    async def timeout(_request):
        raise httpx.ReadTimeout("response timed out")

    transport = _CleanupTransport(timeout)
    with pytest.raises(TargetRequestError) as captured:
        await request(policy, "bank", "/account", transport=transport)

    assert captured.value.diagnostic["execution_uncertain"] is True
    assert captured.value.execution_terminated is False
    assert transport.close_calls == 1


async def test_postheader_timeout_marks_termination_after_stream_and_client_close(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])
    body_stream = _FailingBodyStream()

    async def timeout(_request):
        return httpx.Response(200, stream=body_stream)

    transport = _CleanupTransport(timeout)
    with pytest.raises(TargetRequestError) as captured:
        await request(policy, "bank", "/account", transport=transport)

    assert captured.value.diagnostic["execution_uncertain"] is True
    assert captured.value.execution_terminated is True
    assert body_stream.close_calls == 1
    assert transport.close_calls == 1


async def test_request_error_does_not_mark_termination_when_client_close_fails(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])

    body_stream = _FailingBodyStream()

    async def timeout(_request):
        return httpx.Response(200, stream=body_stream)

    async def fail_close():
        raise OSError("transport close failed")

    transport = _CleanupTransport(timeout, fail_close)
    with pytest.raises(TargetRequestError) as captured:
        await request(policy, "bank", "/account", transport=transport)

    assert captured.value.diagnostic["execution_uncertain"] is True
    assert captured.value.execution_terminated is False
    assert body_stream.close_calls == 1
    assert transport.close_calls == 1


async def test_request_error_does_not_mark_termination_when_client_close_times_out(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])
    bounded_policy = policy.model_copy(update={"timeout_seconds": 0.02})

    body_stream = _FailingBodyStream()

    async def timeout(_request):
        return httpx.Response(200, stream=body_stream)

    async def hang_close():
        await asyncio.Event().wait()

    transport = _CleanupTransport(timeout, hang_close)
    with pytest.raises(TargetRequestError) as captured:
        await request(bounded_policy, "bank", "/account", transport=transport)

    assert captured.value.diagnostic["execution_uncertain"] is True
    assert captured.value.execution_terminated is False
    assert body_stream.close_calls == 1
    assert transport.close_calls == 1


async def test_cancel_during_client_close_propagates_without_detached_cleanup(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    bounded_policy = policy.model_copy(update={"timeout_seconds": 0.05})
    close_started = asyncio.Event()

    body_stream = _FailingBodyStream()

    async def timeout(_request):
        return httpx.Response(200, stream=body_stream)

    async def wait_for_close_cancel():
        close_started.set()
        await asyncio.Event().wait()

    transport = _CleanupTransport(timeout, wait_for_close_cancel)
    task = asyncio.create_task(request(
        bounded_policy, "bank", "/account", transport=transport,
    ))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.close_calls == 1
    assert task.done()


async def test_response_stream_close_failure_prevents_termination_proof(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redteam.capability.servers.network import TargetRequestError

    mock_dns(monkeypatch, ["192.0.2.10"])
    body_stream = _FailingBodyStream(close_fails=True)

    async def response(_request):
        return httpx.Response(200, stream=body_stream)

    transport = _CleanupTransport(response)
    with pytest.raises(TargetRequestError) as captured:
        await request(policy, "bank", "/account", transport=transport)

    assert captured.value.diagnostic["execution_uncertain"] is True
    assert captured.value.execution_terminated is False
    assert body_stream.close_calls == 1
    assert transport.close_calls == 1


async def test_preloaded_response_error_does_not_infer_termination_from_consumed_flag(
    policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    bounded_policy = policy.model_copy(update={"max_response_bytes": 1})
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"too large")
    )

    with pytest.raises(ToolInputError, match="response exceeds configured byte limit") as captured:
        await request(bounded_policy, "bank", "/account", transport=transport)

    assert not hasattr(captured.value, "execution_terminated")


@pytest.fixture
def policy() -> ServerPolicy:
    return ServerPolicy.model_validate(
        {
            "targets": {"bank": {"origin": "https://bank.test", "allowed_paths": ["/account"]}},
            "allowed_networks": ["192.0.2.0/24"],
            "sessions": {
                "alice": {"target_ref": "bank", "principal_ref": "alice", "bearer_env": "LAB_TOKEN"}
            },
        }
    )


def mock_dns(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> None:
    async def getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


async def test_pins_ip_preserves_sni_and_host_and_does_not_leak_token(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    monkeypatch.setenv("LAB_TOKEN", "credential-must-not-appear")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.url.host == "192.0.2.10"
        assert req.headers["Host"] == "bank.test:443"
        assert req.extensions["sni_hostname"] == "bank.test"
        assert req.headers["Authorization"] == "Bearer credential-must-not-appear"
        return httpx.Response(
            200,
            headers=[("Set-Cookie", "s=secret; HttpOnly"), ("Set-Cookie", "t=secret2; Secure")],
            json={"token": "secret", "account": "123456"},
        )

    response = await request(
        policy, "bank", "/account", session_ref="alice", transport=httpx.MockTransport(handler)
    )
    summary = str(response.summary())
    assert len(calls) == 1
    assert "secret" not in summary and "123456" not in summary
    assert response.summary()["body_omitted"] is True
    assert response.set_cookie_headers == ("s=secret; HttpOnly", "t=secret2; Secure")


@pytest.mark.parametrize("address", ["169.254.169.254", "127.0.0.1", "192.0.3.1", "0.0.0.0"])
async def test_dns_answers_outside_scope_are_rejected_before_connection(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    mock_dns(monkeypatch, ["192.0.2.10", address])
    with pytest.raises(ToolInputError):
        await resolve_target(policy, "bank")


@pytest.mark.parametrize(
    "path", ["/transfer", "//other.test", "/account/../transfer", "/%61ccount"]
)
async def test_unapproved_paths_denied(policy: ServerPolicy, path: str) -> None:
    with pytest.raises(ToolInputError, match="path outside"):
        await request(policy, "bank", path)


async def test_preflight_rejection_proves_no_http_request_attempt(policy: ServerPolicy) -> None:
    from redteam.capability.servers.network import TargetRequestError

    called = False

    def fail_if_called(_req):
        nonlocal called
        called = True
        raise AssertionError("preflight rejection reached HTTP transport")

    with pytest.raises(TargetRequestError, match="path outside") as captured:
        await request(
            policy,
            "bank",
            "/outside-scope",
            transport=httpx.MockTransport(fail_if_called),
        )

    assert not called
    assert captured.value.diagnostic["http_request_attempted"] is False
    assert captured.value.diagnostic["execution_uncertain"] is False
    assert captured.value.diagnostic["phase"] == "preflight"


async def test_write_method_disabled_by_default(policy: ServerPolicy) -> None:
    with pytest.raises(ToolInputError, match="method is not enabled"):
        await request(policy, "bank", "/account", method="POST")


async def test_path_specific_method_does_not_enable_writes_elsewhere(
    policy: ServerPolicy,
) -> None:
    config = policy.model_dump()
    config["targets"]["bank"]["allowed_paths"].append("/validate")
    config["targets"]["bank"]["allowed_path_methods"] = {"/validate": ["POST"]}
    scoped = ServerPolicy.model_validate(config)

    with pytest.raises(ToolInputError, match="method is not enabled"):
        await request(scoped, "bank", "/account", method="POST")


async def test_cross_target_session_denied_before_dns(policy: ServerPolicy) -> None:
    config = policy.model_dump()
    config["targets"]["other"] = {"origin": "https://other.test", "allowed_paths": ["/account"]}
    with pytest.raises(ToolInputError, match="not bound"):
        await request(ServerPolicy.model_validate(config), "other", "/account", session_ref="alice")


async def test_redirect_not_followed(policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    response = await request(policy, "bank", "/account", transport=httpx.MockTransport(handler))
    assert response.status == 302 and len(calls) == 1
    assert "169.254" not in str(response.summary())


@pytest.mark.parametrize(
    "headers,content", [({}, b"x" * 1025), ({"Content-Encoding": "gzip"}, b"")]
)
async def test_rejects_large_and_compressed_responses(
    policy: ServerPolicy,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    content: bytes,
) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    config = policy.model_copy(update={"max_response_bytes": 1024})
    with pytest.raises(ToolInputError):
        await request(
            config,
            "bank",
            "/account",
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, headers=headers, content=content)
            ),
        )


async def test_no_automatic_retry(policy: ServerPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_dns(monkeypatch, ["192.0.2.10"])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ReadTimeout("do not return raw request or token")

    with pytest.raises(ToolInputError, match="target HTTP request failed"):
        await request(policy, "bank", "/account", transport=httpx.MockTransport(handler))
    assert len(calls) == 1
