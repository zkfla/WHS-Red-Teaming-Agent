"""Run one lab MCP role; defaults to stdio, with HTTP for persistent local services."""

import argparse

from redteam.capability.servers import active, nuclei, proxy, recon, web_http, zap
from redteam.capability.servers.config import ServerPolicy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=[
        "web-http", "web-proxy", "recon-passive", "recon-active", "web-zap", "web-nuclei",
    ])
    parser.add_argument("--policy", required=True)
    parser.add_argument("--policy-sha256", help="Require exact operator-generated policy bytes")
    parser.add_argument("--capture-path")
    parser.add_argument("--zap-api-origin")
    parser.add_argument("--zap-api-key-env", default="ZAP_API_KEY")
    parser.add_argument("--nuclei-binary", default="nuclei")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    policy = ServerPolicy.load(args.policy, expected_sha256=args.policy_sha256)
    if args.role == "web-proxy":
        if not args.capture_path:
            parser.error("web-proxy requires --capture-path")
        server = proxy.create_server(policy, args.capture_path)
    elif args.role == "web-zap":
        if not args.zap_api_origin:
            parser.error("web-zap requires --zap-api-origin")
        server = zap.create_server(policy, args.zap_api_origin, args.zap_api_key_env)
    elif args.role == "web-nuclei":
        server = nuclei.create_server(policy, args.nuclei_binary)
    else:
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
