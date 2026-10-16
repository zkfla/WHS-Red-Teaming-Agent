"""Target HTTP request metadata distinguishes local preflight from partial calls."""

from mcp import Client

from redteam.capability.servers import recon, web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import (
    CapturedResponse,
    TargetRequestError,
    ToolInputError,
    target_request_rejected,
)
from redteam.core.contracts.tool import (
    PROVIDER_RESULT_EXECUTION_TERMINATED,
    PROVIDER_RESULT_EXECUTION_UNCERTAIN,
)


def _policy() -> ServerPolicy:
    return ServerPolicy.model_validate({
        "targets": {"target": {
            "origin": "https://bank.test",
            "allowed_paths": ["/account"],
        }},
        "allowed_networks": ["192.0.2.0/24"],
        "sessions": {
            "first": {
                "target_ref": "target", "principal_ref": "first",
                "bearer_env": "CODEX_TEST_FIRST_SESSION_TOKEN",
            },
            "second": {
                "target_ref": "target", "principal_ref": "second",
                "bearer_env": "CODEX_TEST_SECOND_SESSION_TOKEN",
            },
        },
    })


async def test_compare_preflight_rejection_proves_no_http_request(monkeypatch):
    monkeypatch.delenv("CODEX_TEST_FIRST_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_TEST_SECOND_SESSION_TOKEN", "fixture-token")
    calls = []

    async def unexpected_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("credential preflight must reject before HTTP")

    monkeypatch.setattr(web_http, "request", unexpected_request)
    async with Client(web_http.create_server(_policy())) as client:
        result = await client.call_tool("http_compare_sessions", {
            "target_ref": "target", "path": "/account",
            "first_session_ref": "first", "second_session_ref": "second",
        })

    assert result.is_error
    assert calls == []
    diagnostic = result.structured_content["diagnostic"]
    assert diagnostic["http_request_attempted"] is False
    assert diagnostic["execution_uncertain"] is False
    assert "execution_started" not in diagnostic
    assert result.meta[PROVIDER_RESULT_EXECUTION_UNCERTAIN] is False
    assert PROVIDER_RESULT_EXECUTION_TERMINATED not in result.meta


async def test_compare_second_preflight_failure_retains_first_completed_request(monkeypatch):
    monkeypatch.setenv("CODEX_TEST_FIRST_SESSION_TOKEN", "fixture-first")
    monkeypatch.setenv("CODEX_TEST_SECOND_SESSION_TOKEN", "fixture-second")
    calls = []

    async def one_success_then_preflight_error(*args, **kwargs):
        calls.append(kwargs["session_ref"])
        if len(calls) == 1:
            return CapturedResponse(200, {}, b"first response")
        raise target_request_rejected(ToolInputError("target DNS resolution failed"))

    monkeypatch.setattr(web_http, "request", one_success_then_preflight_error)
    async with Client(web_http.create_server(_policy())) as client:
        result = await client.call_tool("http_compare_sessions", {
            "target_ref": "target", "path": "/account",
            "first_session_ref": "first", "second_session_ref": "second",
        })

    assert result.is_error
    assert calls == ["first", "second"]
    diagnostic = result.structured_content["diagnostic"]
    assert diagnostic["http_request_attempted"] is True
    assert diagnostic["execution_uncertain"] is False
    assert result.meta[PROVIDER_RESULT_EXECUTION_UNCERTAIN] is False


async def test_target_network_tool_emits_uncertainty_in_result_metadata(monkeypatch):
    async def timeout(*args, **kwargs):
        raise TargetRequestError(
            "target_http_response_timeout",
            "request_or_response",
            "Reconcile before another request.",
            http_request_attempted=True,
            execution_uncertain=True,
        )

    monkeypatch.setattr(web_http, "request", timeout)
    async with Client(web_http.create_server(_policy())) as client:
        result = await client.call_tool("http_request", {
            "target_ref": "target", "path": "/account",
        })

    assert result.is_error
    assert result.meta[PROVIDER_RESULT_EXECUTION_UNCERTAIN] is True
    assert result.structured_content["diagnostic"]["execution_uncertain"] is True


async def test_recon_tool_emits_verified_termination_in_mcp_result_metadata(monkeypatch):
    async def timed_out_after_cleanup(*args, **kwargs):
        raise TargetRequestError(
            "target_http_response_timeout",
            "request_or_response",
            "Reconcile before another request.",
            http_request_attempted=True,
            execution_uncertain=True,
            execution_terminated=True,
        )

    monkeypatch.setattr(recon, "request", timed_out_after_cleanup)
    async with Client(recon.create_server(_policy())) as client:
        result = await client.call_tool("html_inventory", {
            "target_ref": "target", "path": "/account",
        })

    assert result.is_error
    assert result.meta[PROVIDER_RESULT_EXECUTION_UNCERTAIN] is True
    assert result.meta[PROVIDER_RESULT_EXECUTION_TERMINATED] is True
