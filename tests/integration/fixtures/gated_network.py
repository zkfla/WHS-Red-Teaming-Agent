"""Synthetic TCP/UDP fixture for owned, internal Docker networks only.

This is a test input, not an Agent tool. Addresses, ports and artifact paths
are supplied by the proof; probes use literal IPs and bounded attempts.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import threading
import time
from functools import partial
from pathlib import Path


def append(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def bounded_int(value: str, *, maximum: int) -> int:
    number = int(value)
    if not 1 <= number <= maximum:
        raise argparse.ArgumentTypeError(f"must be between 1 and {maximum}")
    return number


def serve(args: argparse.Namespace) -> None:
    directory = Path(args.log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    listeners = (
        (socket.SOCK_STREAM, args.tcp_port, "mock-tcp.log"),
        (socket.SOCK_DGRAM, args.udp_port, "mock-udp.log"),
        (socket.SOCK_STREAM, args.dns_port, "mock-dns-tcp.log"),
        (socket.SOCK_DGRAM, args.dns_port, "mock-dns-udp.log"),
    )
    # Bind all listeners before threads start; a port error must fail startup.
    sockets = []
    for kind, port, name in listeners:
        path = directory / name
        path.write_text("", encoding="utf-8")
        listener = socket.socket(socket.AF_INET, kind)
        listener.bind((args.bind, port))
        if kind == socket.SOCK_STREAM:
            listener.listen(64)
        sockets.append((listener, kind, path))

    def receive(listener: socket.socket, kind: int, path: Path) -> None:
        while True:
            if kind == socket.SOCK_DGRAM:
                body, address = listener.recvfrom(65536)
                append(path, f"DATAGRAM bytes={len(body)}")
                listener.sendto(b"ok", address)
                continue
            connection, _address = listener.accept()
            append(path, "ACCEPT")
            with connection:
                connection.settimeout(1.0)
                try:
                    body = connection.recv(65536)
                    if body:
                        append(path, f"DATA bytes={len(body)}")
                        connection.sendall(b"ok")
                except OSError:
                    append(path, "READ_FAILED")

    for listener, kind, path in sockets:
        threading.Thread(target=receive, args=(listener, kind, path), daemon=True).start()
    threading.Event().wait()


def probe(args: argparse.Namespace) -> None:
    address = ipaddress.ip_address(args.host)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    kind = socket.SOCK_STREAM if args.mode == "tcp" else socket.SOCK_DGRAM
    path = Path(args.log)
    for attempt in range(args.count):
        try:
            with socket.socket(family, kind) as connection:
                connection.settimeout(0.3)
                connection.connect((str(address), args.port))
                connection.sendall(b"x" * args.size)
                # A successful UDP send is not proof of delivery: require a reply.
                if not connection.recv(32):
                    raise OSError("empty reply")
                append(path, f"{attempt} OK")
        except OSError as exc:
            append(path, f"{attempt} FAIL {type(exc).__name__}")
        time.sleep(0.025)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    modes = root.add_subparsers(dest="mode", required=True)
    server = modes.add_parser("serve")
    server.add_argument("--bind", required=True, type=ipaddress.IPv4Address)
    port = partial(bounded_int, maximum=65535)
    server.add_argument("--tcp-port", required=True, type=port)
    server.add_argument("--udp-port", required=True, type=port)
    server.add_argument("--dns-port", required=True, type=port)
    server.add_argument("--log-dir", required=True)
    for mode in ("tcp", "udp"):
        child = modes.add_parser(mode)
        child.add_argument("host", type=ipaddress.ip_address)
        child.add_argument("port", type=port)
        child.add_argument("count", type=partial(bounded_int, maximum=100))
        child.add_argument("size", type=partial(bounded_int, maximum=65536))
        child.add_argument("log")
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    if arguments.mode == "serve":
        arguments.bind = str(arguments.bind)
        serve(arguments)
    else:
        probe(arguments)
