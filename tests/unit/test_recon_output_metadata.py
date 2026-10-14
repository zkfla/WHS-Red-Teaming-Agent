"""Structured recon observations contain only values actually known."""

import pytest
from mcp import Client

from redteam.capability.servers import recon
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse


def test_http_probe_summary_exposes_endpoint_identity_and_status() -> None:
    response = CapturedResponse(
        200,
        {},
        b"ok",
        host="bank.test",
        port=443,
        scheme="https",
        protocol="HTTP/1.1",
        url="https://bank.test/health",
    )

    summary = response.summary()

    assert summary["host"] == "bank.test"
    assert summary["port"] == 443
    assert summary["scheme"] == "https"
    assert summary["protocol"] == "HTTP/1.1"
    assert summary["service"] == "http"
    assert summary["status"] == 200
    assert summary["url"] == "https://bank.test/health"


def test_unknown_recon_metadata_is_not_guessed() -> None:
    summary = CapturedResponse(503, {}, b"").summary()

    assert "host" not in summary
    assert "port" not in summary
    assert "protocol" not in summary


@pytest.mark.parametrize("receipted", [True, False])
async def test_html_inventory_preserves_only_its_own_actual_receipt(monkeypatch, receipted):
    calls = []
    policy = ServerPolicy.model_validate({
        "targets": {"synthetic": {"origin": "https://bank.test", "allowed_paths": ["/"]}},
        "allowed_networks": ["192.0.2.0/24"],
    })

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return CapturedResponse(
            200, {"content-type": "text/html"}, b'<a href="/login">login</a>',
            host="bank.test", port=443, scheme="https", url="https://bank.test/",
            has_session_receipt=receipted,
        )

    monkeypatch.setattr(recon, "request", capture)
    async with Client(recon.create_server(policy)) as client:
        result = await client.call_tool("html_inventory", {"target_ref": "synthetic", "path": "/"})
    assert not result.is_error
    output = result.structured_content
    assert output["status_code"] == 200 and output["url"] == "https://bank.test/"
    assert output["source_sha256"] == output["body_sha256"]
    assert output["request_count"] == len(calls) == 1
    assert output["body_omitted"] is True and "body_preview" not in output
    assert ("actual_session_ref" in output) is receipted
    if receipted:
        assert output["actual_session_ref"] is None
