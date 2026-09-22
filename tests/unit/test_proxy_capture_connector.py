"""ZAP and Burp connector normalization into the neutral proxy catalog."""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import httpx
import pytest

from redteam.capability.connectors.proxy_capture import (
    convert_burp_xml,
    fetch_zap_har,
    normalize_capture,
    write_capture_atomic,
)
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError
from redteam.capability.servers.proxy import HarCaptureBackend


@pytest.fixture
def policy() -> ServerPolicy:
    return ServerPolicy.model_validate(
        {
            "targets": {
                "bank": {
                    "origin": "https://bank.test",
                    "allowed_paths": ["/account", "/validate"],
                    "allowed_path_methods": {"/validate": ["POST"]},
                    "replayable_paths": ["/account", "/validate"],
                    "mutable_json_fields": {"/validate": ["amount"]},
                }
            },
            "allowed_networks": ["192.0.2.0/24"],
            "proxy_max_flows": 10,
        }
    )


def har_document() -> bytes:
    return json.dumps(
        {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://bank.test/account",
                            "headers": [
                                {"name": "Authorization", "value": "captured-token"},
                                {"name": "Accept", "value": "application/json"},
                            ],
                            "cookies": [{"name": "sid", "value": "captured-cookie"}],
                        },
                        "response": {
                            "status": 200,
                            "headers": [
                                {"name": "Set-Cookie", "value": "response-cookie"},
                                {"name": "Cache-Control", "value": "no-store"},
                            ],
                            "cookies": [{"name": "sid", "value": "response-cookie"}],
                            "content": {"text": "synthetic"},
                        },
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://bank.test/validate",
                            "headers": [],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "text": "amount=1",
                            },
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {"text": "ignored"},
                        },
                    },
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://outside.test/tracker",
                            "headers": [],
                        },
                        "response": {
                            "status": 204,
                            "headers": [],
                            "content": {"text": ""},
                        },
                    },
                ],
            }
        }
    ).encode()


async def test_zap_export_uses_fixed_endpoint_key_header_and_bounds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/OTHER/exim/other/exportHar/"
        assert request.url.params["baseurl"] == "https://bank.test"
        assert request.url.params["start"] == "0"
        assert request.url.params["count"] == "10"
        assert request.headers["X-ZAP-API-Key"] == "test-zap-key"
        return httpx.Response(200, content=har_document())

    result = await fetch_zap_har(
        "http://zap:8080",
        "https://bank.test",
        "test-zap-key",
        10,
        transport=httpx.MockTransport(handler),
    )
    assert result == har_document()


async def test_zap_export_does_not_follow_redirect() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "http://outside.test"})
    )
    with pytest.raises(ToolInputError, match="export failed"):
        await fetch_zap_har(
            "http://zap:8080",
            "https://bank.test",
            "test-zap-key",
            10,
            transport=transport,
        )


def test_normalization_removes_credentials_and_unsupported_or_out_of_scope_flows(
    policy: ServerPolicy,
    tmp_path: Path,
) -> None:
    output = tmp_path / "catalog.har"
    count = write_capture_atomic(har_document(), output, policy)
    written = output.read_text(encoding="utf-8")

    assert count == 1
    assert "captured-token" not in written
    assert "captured-cookie" not in written
    assert "response-cookie" not in written
    assert "outside.test" not in written
    assert "amount=1" not in written
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert len(HarCaptureBackend.load(str(output), policy).list_flows()) == 1


def test_burp_saved_items_convert_without_executing_requests(policy: ServerPolicy) -> None:
    request = (
        b"GET /account HTTP/1.1\r\nHost: bank.test\r\nAuthorization: Bearer burp-captured\r\n\r\n"
    )
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Set-Cookie: sid=burp-cookie\r\n\r\n"
        b'{"owner":"synthetic"}'
    )
    xml = (
        "<items><item><url>https://bank.test/account</url>"
        f'<request base64="true">{base64.b64encode(request).decode()}</request>'
        f'<response base64="true">{base64.b64encode(response).decode()}</response>'
        "</item></items>"
    ).encode()

    normalized = normalize_capture(convert_burp_xml(xml), policy)
    exported = normalized.decode()
    backend = HarCaptureBackend.from_bytes(normalized, policy)

    assert len(backend.list_flows()) == 1
    assert "burp-captured" not in exported
    assert "burp-cookie" not in exported
    assert backend.list_flows()[0].captured_response.body == b'{"owner":"synthetic"}'


def test_burp_xml_entities_are_rejected() -> None:
    with pytest.raises(ToolInputError, match="declarations"):
        convert_burp_xml(b'<!DOCTYPE items [<!ENTITY x "secret">]><items>&x;</items>')


def test_empty_or_symlink_output_does_not_replace_catalog(
    policy: ServerPolicy,
    tmp_path: Path,
) -> None:
    output = tmp_path / "catalog.har"
    with pytest.raises(ToolInputError, match="no supported"):
        write_capture_atomic(
            b'{"log":{"entries":[]}}',
            output,
            policy,
            "bank",
        )
    assert not output.exists()

    real_output = tmp_path / "real.har"
    real_output.write_bytes(har_document())
    output.symlink_to(real_output)
    with pytest.raises(ToolInputError, match="symbolic link"):
        write_capture_atomic(har_document(), output, policy, "bank")
