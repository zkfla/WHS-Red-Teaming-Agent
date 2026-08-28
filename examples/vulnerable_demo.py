"""Local-only intentionally vulnerable target for the bounded prototype demo.

Never expose this process to another machine. It binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "WHSVulnerableDemo/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/":
            self._html(
                200,
                """<html><body>
                <h1>WHS local vulnerable demo</h1>
                <a href="/sql?id=1">SQL item</a>
                <a href="/reflect?q=safe">Reflected search</a>
                </body></html>""",
            )
            return
        if parsed.path == "/sql":
            value = query.get("id", ["1"])[0]
            database = sqlite3.connect(":memory:")
            try:
                database.executescript(
                    "CREATE TABLE products (id INTEGER, name TEXT);"
                    "INSERT INTO products VALUES (1, 'prototype');"
                )
                # Intentionally unsafe for the local demonstration target only.
                rows = database.execute(
                    f"SELECT name FROM products WHERE id = {value}"
                ).fetchall()
            except sqlite3.Error as exc:
                self._text(500, f"sqlite3.OperationalError: {exc}")
            else:
                self._text(200, "\n".join(row[0] for row in rows) or "empty")
            finally:
                database.close()
            return
        if parsed.path == "/reflect":
            value = query.get("q", [""])[0]
            # Intentionally unescaped for the local demonstration target only.
            self._html(200, f"<html><body><div id='result'>{value}</div></body></html>")
            return
        self._text(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        self.send_header("allow", "GET, HEAD, OPTIONS")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[demo] {format % args}")

    def _html(self, status: int, body: str) -> None:
        self._send(status, "text/html; charset=utf-8", body)

    def _text(self, status: int, body: str) -> None:
        self._send(status, "text/plain; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler)
    print(f"Intentionally vulnerable local demo: http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
