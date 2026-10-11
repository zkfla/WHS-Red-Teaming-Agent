"""Pinned forward proxy engine. Owned by Capability (TASK-005)."""
from __future__ import annotations

import ipaddress
import socket
import threading

DENIED = b"HTTP/1.1 403 denied\r\nContent-Length: 0\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection established\r\n\r\n"
MAX_HEAD = 65536


class ProxyRefused(ValueError):
    """A refused forwarding attempt."""


def _norm_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


class PinningForwardProxy:
    """CONNECT plus absolute-form GET to pinned IPs only, never DNS."""
    def __init__(self, pins, *, allow_ports=(80, 443, 8080)):
        cleaned = {}
        for host, ips in pins.items():
            if not host:
                raise ProxyRefused("empty pin hostname")
            lits = []
            for ip in ips:
                lits.append(str(ipaddress.ip_address(ip)))
            if not lits:
                raise ProxyRefused("pin without ips")
            cleaned[_norm_host(host)] = tuple(lits)
        self._pins = cleaned
        self._ports = frozenset(allow_ports)
        self._lock = threading.Lock()
        self._counts = {"connections": 0, "relayed_bytes": 0, "refused": 0}
        self._server = None
        self._stop = False
    @property
    def pins(self):
        return dict(self._pins)
    def counts(self):
        with self._lock:
            return dict(self._counts)
    def _bump(self, key, amount=1):
        with self._lock:
            self._counts[key] += amount
    def start(self, host="127.0.0.1", port=0):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(50)
        server.settimeout(1.0)
        self._server = server
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return server.getsockname()[1]
    def stop(self):
        self._stop = True
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
    def _serve(self):
        while not getattr(self, "_stop", True):
            try:
                conn, _peer = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
    def _read_head(self, conn):
        conn.settimeout(15.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_HEAD:
                raise ProxyRefused("header too large")
        return buf
    def _target(self, method, target):
        if method == "CONNECT":
            host, _, port_text = target.partition(":")
            port = int(port_text)
        elif target.startswith("http://"):
            rest = target[len("http://"):]
            hostport, _, _path = rest.partition("/")
            host, _, port_text = hostport.partition(":")
            port = int(port_text) if port_text else 80
        else:
            raise ProxyRefused("unsupported form")
        return _norm_host(host), port
    def _dial(self, host, port):
        ips = self._pins.get(host)
        if ips is None:
            raise ProxyRefused("unknown host")
        if port not in self._ports:
            raise ProxyRefused("port not allowed")
        for ip in ips:
            try:
                sock = socket.create_connection((ip, port), timeout=10)
                self._bump("connections")
                return sock
            except OSError:
                pass  # try next pinned IP
        raise ProxyRefused("dial failed")
    def _relay(self, left, right):
        left.settimeout(30.0)
        right.settimeout(30.0)
        total = 0
        try:
            while True:
                try:
                    chunk = left.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                right.sendall(chunk)
                total += len(chunk)
        finally:
            self._bump("relayed_bytes", total)
    def _handle(self, conn):
        upstream = None
        try:
            try:
                head = self._read_head(conn)
            except ProxyRefused:
                conn.sendall(DENIED)
                self._bump("refused")
                return
            first, _, _ = head.partition(b"\r\n")
            parts = first.decode("latin-1").split(" ")
            if len(parts) < 2:
                conn.sendall(DENIED)
                self._bump("refused")
                return
            method, target = parts[0].upper(), parts[1]
            try:
                host, port = self._target(method, target)
                upstream = self._dial(host, port)
            except (ProxyRefused, ValueError):
                try:
                    conn.sendall(DENIED)
                except OSError:
                    pass
                self._bump("refused")
                return
            if method == "CONNECT":
                conn.sendall(ESTABLISHED)
            else:
                segs = target.split("/", 3)
                origin = "/" + segs[3] if len(segs) > 3 else "/"
                version = parts[2] if len(parts) > 2 else "HTTP/1.0"
                at = head.index(b"\r\n")
                request_line = (
                    method.encode() + b" "
                    + origin.encode() + b" "
                    + version.encode() + head[at:]
                )
                upstream.sendall(request_line)
            worker = threading.Thread(target=self._relay, args=(upstream, conn), daemon=True)
            worker.start()
            self._relay(conn, upstream)
            worker.join(timeout=60)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
