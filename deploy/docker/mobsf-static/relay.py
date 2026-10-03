"""Loopback-published relay to the isolated MobSF service; no arbitrary destinations."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PATHS = frozenset({"/api/v1/upload", "/api/v1/scan", "/api/v1/scan_logs", "/api/v1/report_json"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        if self.path not in _PATHS or (self.command == "GET" and self.path != "/api/v1/upload"):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 200_000_000 or self.headers.get("Transfer-Encoding"):
                self.send_error(413)
                return
            self.connection.settimeout(620)
            body = self.rfile.read(length)
            headers = {
                key: self.headers[key]
                for key in ("Content-Type", "Authorization")
                if self.headers.get(key)
            }
            conn = http.client.HTTPConnection("mobsf-static", 8000, timeout=610)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                payload = response.read(33_554_433)
                if len(payload) > 33_554_432:
                    self.send_error(502)
                    return
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                conn.close()
        except (OSError, ValueError, http.client.HTTPException):
            self.send_error(503)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
