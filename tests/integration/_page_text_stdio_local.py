"""Test-only stdio MCP process for the local HTTPS page reader integration test.

The socket remap and certificate trust exist only here, outside production settings.
"""

from __future__ import annotations

import argparse
import ssl
from typing import Any

import httpx
from httpcore import AsyncNetworkStream

from redteam.capability.servers.page_text import PageTextReader
from redteam.capability.servers.page_text_mcp import create_server, load_page_text_config

PUBLIC_IP = "93.184.215.14"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--certificate", required=True)
    args = parser.parse_args()
    config, _ = load_page_text_config(args.config, args.sha256)
    trusted_context = ssl.create_default_context(cafile=args.certificate)

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        assert host == "docs.example" and port == args.port
        return (PUBLIC_IP,)

    def transport_factory() -> httpx.AsyncHTTPTransport:
        transport = httpx.AsyncHTTPTransport(
            verify=trusted_context, trust_env=False, retries=0, http2=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        backend = transport._pool._network_backend
        original_connect = backend.connect_tcp

        async def connect_local(host: str, port: int, **kwargs: Any) -> AsyncNetworkStream:
            # Production PageTextReader still chooses the checked public IP.
            # Only this test process remaps the final TCP socket to its local server.
            assert host == PUBLIC_IP and port == args.port
            return await original_connect("127.0.0.1", args.port, **kwargs)

        backend.connect_tcp = connect_local  # type: ignore[method-assign, assignment]
        return transport

    reader = PageTextReader(config, resolver=resolve, transport_factory=transport_factory)
    create_server(config, reader=reader).run(transport="stdio")


if __name__ == "__main__":
    main()
