"""Run one lab MCP role; defaults to stdio, with HTTP for persistent local services."""

import argparse

from redteam.capability.servers import active, recon, web_http
from redteam.capability.servers.config import ServerPolicy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["web-http", "recon-passive", "recon-active"])
    parser.add_argument("--policy", required=True)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    policy = ServerPolicy.load(args.policy)
    factory = {"web-http": web_http, "recon-passive": recon, "recon-active": active}[args.role]
    server = factory.create_server(policy)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
