"""Read-only synthetic bank for local tests. No payment/transfer endpoints."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 200
        content_type = "application/json"
        if self.path == "/":
            content_type = "text/html; charset=utf-8"
            body = (
                b'<html><title>Finance MCP lab</title><h1>Synthetic bank</h1>'
                b'<label>Test alias<input id="alias"></label>'
                b'<button id="confirm" onclick="document.getElementById(\'result\').textContent='
                b'document.getElementById(\'alias\').value">Preview alias</button>'
                b'<p id="result"></p></html>'
            )
        elif self.path == "/health":
            body = b'{"status":"ok"}'
        elif self.path == "/api/accounts/alice":
            if self.headers.get("Authorization") == "Bearer synthetic-alice":
                body = b'{"owner":"test-alice","balance":1000,"currency":"KRW"}'
            else:
                status = 403
                body = b'{"error":"forbidden"}'
        elif self.path == "/openapi.json":
            body = json.dumps(
                {
                    "openapi": "3.0.3",
                    "paths": {
                        "/api/accounts/alice": {"get": {"security": [{"bearerAuth": []}]}},
                    },
                }
            ).encode()
        elif self.path == "/.well-known/openid-configuration":
            body = json.dumps(
                {
                    "issuer": "http://mock-bank:8080",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["private_key_jwt"],
                }
            ).encode()
        else:
            status = 404
            body = b'{"error":"not_found"}'
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
