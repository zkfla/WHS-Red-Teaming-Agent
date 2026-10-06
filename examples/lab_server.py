"""Local-only HTTP fixture for testing the redteaming agent.

This server intentionally binds to loopback by default and is not an Internet
target. It has no external dependencies.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class LabHandler(BaseHTTPRequestHandler):
    server_version = "RedteamingLab/0.1"

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send_json(200, {"service": "redteaming-lab", "status": "ok"})
        elif parsed.path == "/healthz":
            self._send_json(200, {"healthy": True})
        elif parsed.path == "/api/items":
            item_id = query.get("id", ["1"])[0]
            items = {"1": {"id": "1", "name": "demo", "classification": "public"},
                     "2": {"id": "2", "name": "fixture", "classification": "test-only"}}
            item = items.get(item_id)
            if item is None:
                self._send_json(404, {"error": "item_not_found"})
            else:
                self._send_json(200, item)
        else:
            self._send_json(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[lab-server] {self.address_string()} - {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback-only redteaming test server")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LabHandler)
    print(f"Redteaming lab server: http://{args.host}:{args.port}")
    print("Scope this exact host as 127.0.0.1. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping lab server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
