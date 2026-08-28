import asyncio
from pathlib import Path

import httpx
import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import ScopeViolation
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.http_client import SafeHttpClient
from whs_redteam.tools.network_gate import NetworkGate


def make_client(
    tmp_path: Path,
    handler: httpx.MockTransport,
    *,
    max_body_bytes: int = 262_144,
) -> SafeHttpClient:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    logger = AuditLogger(tmp_path / "events.jsonl")
    gate = NetworkGate(ScopeGuard(config, logger), RequestBudget(20, 20, 60), logger)
    return SafeHttpClient(
        gate,
        logger,
        transport=handler,
        max_body_bytes=max_body_bytes,
    )


def test_response_body_is_capped(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"a" * 100))
    client = make_client(tmp_path, transport, max_body_bytes=16)
    response = asyncio.run(client.get("https://example.test/data"))
    assert len(response.body) == 16


def test_redirect_is_rechecked_by_scope_guard(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://outside.test/result"})

    client = make_client(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(ScopeViolation):
        asyncio.run(client.get("https://example.test/start"))


def test_safe_redirect_is_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="ok")

    client = make_client(tmp_path, httpx.MockTransport(handler))
    response = asyncio.run(client.get("https://example.test/start"))
    assert response.status_code == 200
    assert response.url == "https://example.test/final"


def test_invalid_charset_falls_back_to_utf8(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=not-a-real-charset"},
            content="안전한 응답".encode(),
        )
    )
    response = asyncio.run(make_client(tmp_path, transport).get("https://example.test"))
    assert response.text == "안전한 응답"


def test_transaction_fingerprints_are_generated_without_secret_values(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="response"))
    response = asyncio.run(
        make_client(tmp_path, transport).get(
            "https://example.test/data",
            headers={"Authorization": "Bearer secret-token"},
        )
    )
    assert len(response.request_fingerprint) == 64
    assert len(response.response_fingerprint) == 64
    assert "secret-token" not in response.request_fingerprint
