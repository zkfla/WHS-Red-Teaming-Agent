"""Prove IP pinning retains TLS SNI and certificate verification on a local TLS server."""

import asyncio
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from redteam.capability.servers import network, recon
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ResolvedTarget, ToolInputError


async def test_https_pin_and_tls_inspect_verify_hostname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL needed for ephemeral test certificate")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=bank.test",
            "-addext",
            "subjectAltName=DNS:bank.test",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    seen_sni = []
    seen_requests = []
    server_context.set_servername_callback(lambda sock, name, context: seen_sni.append(name))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(4096)
            if data:
                seen_requests.append(data)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                await writer.drain()
        finally:
            writer.close()

    try:
        listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    except PermissionError:
        pytest.skip("local TCP port unavailable in sandbox")
    async with listener:
        port = listener.sockets[0].getsockname()[1]
        policy = ServerPolicy.model_validate(
            {
                "targets": {
                    "bank": {"origin": f"https://bank.test:{port}", "allowed_paths": ["/"]}
                },
                "allowed_networks": ["127.0.0.0/8"],
            }
        )

        async def resolve(*args: Any) -> ResolvedTarget:
            return ResolvedTarget(policy.targets["bank"], "bank.test", port, ("127.0.0.1",))

        monkeypatch.setattr(network, "resolve_target", resolve)
        monkeypatch.setattr(recon, "resolve_target", resolve)
        trusted_context = ssl.create_default_context(cafile=str(certificate))
        response = await network.request(
            policy,
            "bank",
            "/",
            transport=httpx.AsyncHTTPTransport(verify=trusted_context),
        )
        assert response.status == 200
        assert seen_sni[0] == "bank.test"
        assert f"Host: bank.test:{port}".encode() in seen_requests[0]
        # Default trust store must reject this self-signed certificate.
        with pytest.raises(ToolInputError, match="HTTP request failed"):
            await network.request(policy, "bank", "/")
        monkeypatch.setattr(recon.ssl, "create_default_context", lambda: trusted_context)
        async with Client(recon.create_server(policy)) as client:
            result = await client.call_tool("tls_inspect", {"target_ref": "bank"})
        assert result.structured_content is not None
        assert result.structured_content["verified"] is True
        assert len(result.structured_content["certificate_sha256"]) == 64
