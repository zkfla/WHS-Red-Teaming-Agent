"""Local TLS/socket checks for the page reader; no external host is contacted."""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpcore import AsyncNetworkStream

from redteam.capability.servers.page_text import (
    PageTextConfig,
    PageTextError,
    PageTextErrorCode,
    PageTextReader,
    PageTextRequest,
)

PUBLIC_IP = "93.184.215.14"
HOST = "docs.example"


def _certificate(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required for the local TLS test")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=docs.example", "-addext", "subjectAltName=DNS:docs.example",
            "-keyout", str(key), "-out", str(certificate),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    return server_context, ssl.create_default_context(cafile=str(certificate))


def _reader(
    port: int, context: ssl.SSLContext | bool, connected: list[tuple[str, int]],
    *, timeout: float = 2.0, origins: tuple[str, ...] | None = None,
) -> PageTextReader:
    async def resolve(host: str, requested_port: int) -> tuple[str, ...]:
        assert requested_port == port
        return (PUBLIC_IP,)

    def factory() -> httpx.AsyncHTTPTransport:
        transport = httpx.AsyncHTTPTransport(
            verify=context, trust_env=False, retries=0, http2=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        backend = transport._pool._network_backend
        original_connect = backend.connect_tcp
        expected_port = port

        async def local_connect(
            host: str, port: int, **kwargs: Any,
        ) -> AsyncNetworkStream:
            # Remap only at the final socket boundary; HTTPX/httpcore still see
            # the verified public IP, and real TCP/TLS runs on the loopback server.
            connected.append((host, port))
            assert host == PUBLIC_IP and port == expected_port
            return await original_connect("127.0.0.1", expected_port, **kwargs)

        backend.connect_tcp = local_connect  # type: ignore[method-assign, assignment]
        return transport

    return PageTextReader(
        PageTextConfig(
            allowed_origins=origins or (f"https://{HOST}:{port}",),
            timeout_seconds=timeout,
        ),
        resolver=resolve, transport_factory=factory,
    )


async def test_real_tls_pin_host_cert_redirect_and_connection_cleanup(tmp_path: Path) -> None:
    server_context, trusted_context = _certificate(tmp_path)
    sni: list[str | None] = []
    requests: list[bytes] = []
    closed: list[asyncio.Event] = []
    server_context.set_servername_callback(lambda _sock, host, _context: sni.append(host))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        was_closed = asyncio.Event()
        closed.append(was_closed)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            if request.startswith(b"GET /start "):
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: /end\r\nContent-Length: 0\r\n\r\n")
            elif request.startswith(b"GET /blocked "):
                writer.write(
                    (f"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1:{port}/private\r\n"
                     "Content-Length: 0\r\n\r\n").encode()
                )
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Content-Length: 2\r\n\r\nok")
            await writer.drain()
            await reader.read()  # Observe the client close each transport.
            was_closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        connected: list[tuple[str, int]] = []
        url = f"https://{HOST}:{port}/start"
        # Single-hop contract: /start returns the unvisited redirect target;
        # the destination safety check runs on the next explicit request.
        result = await _reader(port, trusted_context, connected).fetch_page_text(
            PageTextRequest(url=url)
        )
        assert result.status_code == 302
        assert result.final_url == url
        assert result.redirect_url == f"https://{HOST}:{port}/end"
        assert result.body_present is False and result.text == ""
        assert connected == [(PUBLIC_IP, port)]
        assert sni == [HOST]
        assert len(requests) == 1
        assert f"Host: {HOST}:{port}\r\n".encode() in requests[0]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in closed)), 1)
        follow = await _reader(port, trusted_context, connected).fetch_page_text(
            PageTextRequest(url=result.redirect_url or f"https://{HOST}:{port}/end")
        )
        assert follow.text == "ok" and follow.final_url == f"https://{HOST}:{port}/end"
        assert follow.body_present is True
        assert connected == [(PUBLIC_IP, port), (PUBLIC_IP, port)]
        assert sni == [HOST, HOST]
        assert len(requests) == 2
        assert all(f"Host: {HOST}:{port}\r\n".encode() in data for data in requests)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in closed)), 1)

        # Even an allowlisted redirect origin cannot bypass the private-IP rule.
        blocked_first = await _reader(
            port, trusted_context, connected,
            origins=(f"https://{HOST}:{port}", f"https://127.0.0.1:{port}"),
        ).fetch_page_text(PageTextRequest(url=f"https://{HOST}:{port}/blocked"))
        assert blocked_first.redirect_url == f"https://127.0.0.1:{port}/private"
        assert blocked_first.body_present is False
        assert connected == [(PUBLIC_IP, port)] * 3
        assert len(requests) == 3
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in closed)), 1)
        with pytest.raises(PageTextError) as forbidden:
            await _reader(
                port, trusted_context, connected,
                origins=(f"https://{HOST}:{port}", f"https://127.0.0.1:{port}"),
            ).fetch_page_text(
                PageTextRequest(
                    url=blocked_first.redirect_url or f"https://127.0.0.1:{port}/private"
                )
            )
        assert forbidden.value.code == PageTextErrorCode.ACCESS_DENIED
        assert connected == [(PUBLIC_IP, port)] * 3
        assert len(requests) == 3
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in closed)), 1)

        # A self-signed certificate must fail with the default trust store.
        with pytest.raises(PageTextError) as untrusted:
            await _reader(port, True, connected).fetch_page_text(PageTextRequest(url=url))
        assert untrusted.value.code == PageTextErrorCode.CONNECTION_ERROR
        assert len(requests) == 3

        # The same trusted certificate must fail for a different URL hostname.
        wrong_url = f"https://other.example:{port}/start"
        with pytest.raises(PageTextError) as wrong_host:
            await _reader(
                port, trusted_context, connected, origins=(f"https://other.example:{port}",)
            ).fetch_page_text(PageTextRequest(url=wrong_url))
        assert wrong_host.value.code == PageTextErrorCode.CONNECTION_ERROR
        assert len(requests) == 3
        assert sni[-1] == "other.example"


@pytest.mark.parametrize("case", ["http_error", "timeout", "cancel"])
async def test_real_tls_closes_socket_on_failure_timeout_and_cancel(
    tmp_path: Path, case: str,
) -> None:
    server_context, trusted_context = _certificate(tmp_path)
    request_arrived = asyncio.Event()
    client_closed = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            request_arrived.set()
            if case == "http_error":
                writer.write(b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Content-Length: 5\r\n\r\na")
            await writer.drain()
            await reader.read()
            client_closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        connected: list[tuple[str, int]] = []
        reader = _reader(port, trusted_context, connected, timeout=0.15)
        url = PageTextRequest(url=f"https://{HOST}:{port}/page")
        if case == "cancel":
            task = asyncio.create_task(reader.fetch_page_text(url))
            await asyncio.wait_for(request_arrived.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(PageTextError) as error:
                await reader.fetch_page_text(url)
            expected = (PageTextErrorCode.HTTP_ERROR if case == "http_error"
                        else PageTextErrorCode.TIMEOUT)
            assert error.value.code == expected
        assert connected == [(PUBLIC_IP, port)]
        await asyncio.wait_for(client_closed.wait(), 1)
