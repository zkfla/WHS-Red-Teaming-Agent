"""In-netns NFQUEUE admission daemon. Owned by Capability (TASK-005)."""

from __future__ import annotations

import os
import select
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

try:
    from redteam.capability.gate_wire import (
        MAX_LINE_BYTES,
        AdmissionReply,
        AdmissionRequest,
        WireError,
        decode_reply,
        encode_request,
    )
except ImportError:
    from gate_wire import (  # type: ignore[no-redef]
        MAX_LINE_BYTES,  # type: ignore[no-redef]
        AdmissionReply,  # type: ignore[no-redef]
        AdmissionRequest,  # type: ignore[no-redef]
        WireError,  # type: ignore[no-redef]
        decode_reply,  # type: ignore[no-redef]
        encode_request,  # type: ignore[no-redef]
    )

REPLY_TIMEOUT_SECONDS = 5.0


class HeldPacket(Protocol):
    packet_id: int
    ip_version: int
    proto: str
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int

    def apply_accept(self) -> None: ...

    def apply_drop(self) -> None: ...


class NFQueueBackend(Protocol):
    def bind(self, queue_num: int, callback: Callable[[HeldPacket], None]) -> None: ...

    def run(self) -> None: ...

    def unbind(self) -> None: ...


@dataclass
class GateDaemon:
    window_id: str
    epoch: int
    daemon_session_id: str
    backend: NFQueueBackend
    stdin: Any = field(default=None)
    stdout: Any = field(default=None)
    reply_timeout_seconds: float = REPLY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self._seq = 0
        self._in_flight: dict[int, str] = {}

    def _read_reply(self) -> AdmissionReply:
        stdin = self.stdin if self.stdin is not None else sys.stdin.buffer
        try:
            fd = stdin.fileno()
        except (OSError, ValueError):
            fd = None
        if fd is not None:
            ready, _, _ = select.select([fd], [], [], self.reply_timeout_seconds)
            if not ready:
                raise WireError("admission reply timeout")
        line = stdin.readline(MAX_LINE_BYTES + 256)
        if not line:
            raise WireError("admission channel closed")
        return decode_reply(line)

    def _request_verdict(self, packet: HeldPacket) -> tuple[bool, str]:
        self._seq += 1
        seq = self._seq
        flow_key = (
            packet.proto
            + ":"
            + str(packet.ip_version)
            + ":"
            + packet.src_ip
            + ":"
            + str(packet.src_port)
            + ">"
            + packet.dst_ip
            + ":"
            + str(packet.dst_port)
        )
        self._in_flight[seq] = flow_key
        try:
            request = AdmissionRequest(
                window_id=self.window_id,
                daemon_session_id=self.daemon_session_id,
                seq=seq,
                epoch=self.epoch,
                flow_key=flow_key,
                ip_version=packet.ip_version,
                proto=packet.proto,
                dst_ip=packet.dst_ip,
                dst_port=packet.dst_port,
            )
            stdout = self.stdout if self.stdout is not None else sys.stdout.buffer
            stdout.write(encode_request(request))
            stdout.flush()
            reply = self._read_reply()
        except (WireError, OSError, ValueError):
            return False, "rpc_failed"
        finally:
            self._in_flight.pop(seq, None)
        if reply.seq != seq:
            return False, "seq_mismatch"
        if reply.accepted:
            try:
                packet.apply_accept()
            except (OSError, ValueError):
                return False, "accept_failed"
            return True, reply.reason
        return False, reply.reason

    def on_packet(self, packet: HeldPacket) -> tuple[bool, str]:
        accepted, reason = self._request_verdict(packet)
        if not accepted:
            try:
                packet.apply_drop()
            except (OSError, ValueError):
                pass
        return accepted, reason

    def serve(self, queue_num: int) -> None:
        self.backend.bind(queue_num, self._handle)
        try:
            self.backend.run()
        finally:
            self.backend.unbind()

    def _handle(self, packet: HeldPacket) -> None:
        """Queue callback discarding the verdict tuple; drops apply inside."""
        self.on_packet(packet)


def daemon_session_id() -> str:
    return uuid.uuid4().hex


def main(argv: list[str]) -> int:
    try:
        window = argv[argv.index("--window") + 1]
        epoch = int(argv[argv.index("--epoch") + 1])
        queue_num = int(argv[argv.index("--queue") + 1])
    except (ValueError, IndexError):
        print("usage: gate-daemon --window ID --epoch N --queue Q", file=sys.stderr)
        return 2
    session = os.environ.get("GATE_DAEMON_SESSION")
    if not session:
        print("GATE_DAEMON_SESSION is required (launch-registered identity)", file=sys.stderr)
        return 4
    try:
        from redteam.capability.gate_nfqueue import RealNFQueueBackend
    except ImportError:
        try:
            from gate_nfqueue import RealNFQueueBackend  # type: ignore[no-redef]
        except ImportError:
            print("nfqueue backend unavailable", file=sys.stderr)
            return 3
    daemon = GateDaemon(
        window_id=window,
        epoch=epoch,
        daemon_session_id=session,
        backend=RealNFQueueBackend(),
    )
    daemon.serve(queue_num)
    return 0
