"""Real NFQUEUE backend. Ships flat with gate_daemon.py in the sidecar."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class RealNFQueueBackend:
    def __init__(self) -> None:
        try:
            from netfilterqueue import NetfilterQueue  # type: ignore
        except ImportError as exc:
            raise ImportError("NetfilterQueue bindings unavailable") from exc
        self._nfqueue = NetfilterQueue()
        self._callback: Callable[..., None] | None = None

    def bind(self, queue_num: int, callback: Callable[..., None]) -> None:
        self._callback = callback
        self._nfqueue.bind(queue_num, self._on_raw, max_len=1024)

    def _on_raw(self, raw: object) -> None:
        assert self._callback is not None
        self._callback(_PacketView(raw))

    def run(self) -> None:
        self._nfqueue.run()

    def unbind(self) -> None:
        try:
            self._nfqueue.unbind()
        except (OSError, RuntimeError):
            pass


class _PacketView:
    def __init__(self, raw: Any) -> None:
        get = getattr
        self._raw = raw
        self.packet_id = 0
        payload = get(raw, "get_payload", lambda: b"")()
        ver = (payload[0] >> 4) if payload else 4
        self.ip_version = int(ver)
        self.proto = "other"
        self.src_ip = "0.0.0.0"
        self.dst_ip = "0.0.0.0"
        self.src_port = 0
        self.dst_port = 0
        if ver == 4 and len(payload) >= 20:
            self.src_ip = ".".join(str(b) for b in payload[12:16])
            self.dst_ip = ".".join(str(b) for b in payload[16:20])
            num = payload[9]
            ihl = (payload[0] & 0x0F) * 4
            if num == 6:
                self.proto = "tcp"
            elif num == 17:
                self.proto = "udp"
            if len(payload) >= ihl + 4:
                self.src_port = int(payload[ihl] * 256 + payload[ihl + 1])
                self.dst_port = int(payload[ihl + 2] * 256 + payload[ihl + 3])

    def apply_accept(self) -> None:
        self._raw.accept()

    def apply_drop(self) -> None:
        self._raw.drop()
