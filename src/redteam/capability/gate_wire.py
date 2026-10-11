"""Provider-owned NFQUEUE admission wire protocol (stdio JSON lines).

Owned by Capability (TASK-005). This module is the provider side of the
Control-owned ``docker exec`` stdio RPC: the gate daemon writes
``AdmissionRequest`` lines on stdout and reads ``AdmissionReply`` lines on
stdin. Control's launch registry owns the pipes; the model container has no
access to them. Core ports never import this module; the Control-side RPC
handler translates wire types to ``core.contracts.network`` types at the
boundary.

stdout is protocol-only; stderr carries bounded human diagnostics that are
never parsed. Every line is size-bounded; oversize or malformed input is a
refusal (drop), never an accept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WIRE_VERSION = 1

# Hard bound for one protocol line, including the trailing newline. The
# largest legal request is a few hundred bytes; anything larger is hostile.
MAX_LINE_BYTES = 4096

# Matches the ledger's ``flow_key`` bound (SQLiteNetworkGrantLedger
# refuses longer keys); the wire enforces it first so oversize input never
# reaches the authority.
MAX_FLOW_KEY_CHARS = 256


class WireError(ValueError):
    """A wire line that must never authorize anything."""


@dataclass(frozen=True)
class AdmissionRequest:
    window_id: str
    daemon_session_id: str
    seq: int
    epoch: int
    flow_key: str
    ip_version: int
    proto: str
    dst_ip: str
    dst_port: int


@dataclass(frozen=True)
class AdmissionReply:
    seq: int
    accepted: bool
    reason: str


def _check_seq(seq: int) -> int:
    if type(seq) is not int or not 0 <= seq < 2**63:
        raise WireError("seq out of range")
    return seq


def encode_request(request: AdmissionRequest) -> bytes:
    """Encode one request line (with trailing newline)."""
    if not request.window_id or not request.daemon_session_id:
        raise WireError("request identity required")
    _check_seq(request.seq)
    if type(request.epoch) is not int or request.epoch < 1:
        raise WireError("epoch out of range")
    if not request.flow_key or len(request.flow_key) > MAX_FLOW_KEY_CHARS:
        raise WireError("flow_key out of range")
    if request.ip_version not in (4, 6):
        raise WireError("ip_version must be 4 or 6")
    if request.proto not in ("tcp", "udp", "other"):
        raise WireError("proto must be tcp, udp or other")
    if not request.dst_ip or len(request.dst_ip) > 64:
        raise WireError("dst_ip out of range")
    if type(request.dst_port) is not int or not 0 <= request.dst_port <= 65535:
        raise WireError("dst_port out of range")
    payload = {
        "v": WIRE_VERSION,
        "window_id": request.window_id,
        "daemon_session_id": request.daemon_session_id,
        "seq": request.seq,
        "epoch": request.epoch,
        "flow_key": request.flow_key,
        "ip_version": request.ip_version,
        "proto": request.proto,
        "dst_ip": request.dst_ip,
        "dst_port": request.dst_port,
    }
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_LINE_BYTES:
        raise WireError("request line too long")
    return line


def decode_request(line: bytes) -> AdmissionRequest:
    """Decode and validate one request line. Raises WireError on anything off."""
    if len(line) > MAX_LINE_BYTES:
        raise WireError("request line too long")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WireError("request is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("v") != WIRE_VERSION:
        raise WireError("unsupported wire version")
    try:
        request = AdmissionRequest(
            window_id=str(payload["window_id"]),
            daemon_session_id=str(payload["daemon_session_id"]),
            seq=payload["seq"],
            epoch=payload["epoch"],
            flow_key=str(payload["flow_key"]),
            ip_version=payload["ip_version"],
            proto=str(payload["proto"]),
            dst_ip=str(payload["dst_ip"]),
            dst_port=payload["dst_port"],
        )
    except (KeyError, TypeError) as exc:
        raise WireError("request field missing or mistyped") from exc
    encode_request(request)
    return request


def encode_reply(reply: AdmissionReply) -> bytes:
    """Encode one reply line (with trailing newline)."""
    _check_seq(reply.seq)
    if not reply.reason or len(reply.reason) > 128:
        raise WireError("reason out of range")
    payload = {
        "v": WIRE_VERSION,
        "seq": reply.seq,
        "accepted": bool(reply.accepted),
        "reason": reply.reason,
    }
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_LINE_BYTES:
        raise WireError("reply line too long")
    return line


def decode_reply(line: bytes) -> AdmissionReply:
    """Decode and validate one reply line. Raises WireError on anything off."""
    if len(line) > MAX_LINE_BYTES:
        raise WireError("reply line too long")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WireError("reply is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("v") != WIRE_VERSION:
        raise WireError("unsupported wire version")
    try:
        reply = AdmissionReply(
            seq=payload["seq"],
            accepted=bool(payload["accepted"]),
            reason=str(payload["reason"]),
        )
    except (KeyError, TypeError) as exc:
        raise WireError("reply field missing or mistyped") from exc
    encode_reply(reply)
    return reply
