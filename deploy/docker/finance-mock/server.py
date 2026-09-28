"""Read-only synthetic bank for local tests. No payment/transfer endpoints."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ponytail: single env gate for the deliberately-vulnerable demo target.
# Unset (default) => secure server, identical to production behaviour.
# VULNERABLE_DEMO=1 => broken object-level auth on /api/accounts/alice
# (any bearer is accepted), so a deny-declared session gets 2xx and the
# agent promotes an ACCESS_CONTROL_VIOLATION finding. Referenced by
# deploy/compose/finance-vulnerable-demo.yml.
VULN = os.environ.get("VULNERABLE_DEMO") == "1"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 200
        content_type = "application/json"
        if self.path == "/":
            content_type = "text/html; charset=utf-8"
            body = (
                b"<html><title>Finance MCP lab</title><h1>Synthetic bank</h1>"
                b'<a href="/api/accounts/alice?trace=synthetic-secret">Account</a>'
                b'<script src="/assets/test-sdk.js"></script>'
                b'<label>Test alias<input id="alias"></label>'
                b'<button id="confirm" onclick="document.getElementById(\'result\').textContent='
                b"document.getElementById('alias').value\">Preview alias</button>"
                b'<p id="result"></p></html>'
            )
        elif urlsplit(self.path).path == "/health":
            body = b'{"status":"ok"}'
        elif self.path == "/api/accounts/alice":
            auth = self.headers.get("Authorization", "")
            authorized = auth == "Bearer synthetic-alice" or (VULN and auth.startswith("Bearer "))
            if authorized:
                body = b'{"owner":"test-alice","balance":1000,"currency":"KRW"}'
            elif VULN:
                # Unauthenticated baseline is 401 so the access-control check
                # (authentication.py requires a 401 anonymous baseline) fires.
                status = 401
                body = b'{"error":"unauthorized"}'
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
        self.send_header(
            "Set-Cookie", "lab_session=synthetic-cookie; Path=/; Secure; HttpOnly; SameSite=Lax"
        )
        self.send_header("Set-Cookie", "lab_preference=synthetic-pref; Path=/; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/api/beneficiaries/validate":
            self._json_response(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                raise ValueError
            document = json.loads(self.rfile.read(length))
            alias = document["account_alias"]
            amount = document["amount"]
            if not isinstance(alias, str) or not isinstance(amount, int):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_request"})
            return
        self._json_response(
            200,
            {
                "valid": bool(alias) and 0 < amount <= 100000,
                "validation_only": True,
            },
        )

    def _json_response(self, status: int, document: dict[str, object]) -> None:
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
