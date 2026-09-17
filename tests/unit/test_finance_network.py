"""Target pinning, auth isolation, bounded output and absence of automatic replay."""

import asyncio
import socket
from typing import Any

import httpx
import pytest

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError, request, resolve_target


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
