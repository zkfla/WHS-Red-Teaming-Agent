"""Structured recon observations contain only values actually known."""

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
