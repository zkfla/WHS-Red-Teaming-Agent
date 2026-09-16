"""Proxy-product-neutral MCP facade over operator-exported HTTP Archive captures."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp.server import MCPServer

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse, ToolInputError, request

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_MAX_REQUEST_BODY_BYTES = 65536


class _UnsupportedRequestBody(Exception):
    """A well-formed capture body that this bounded replay service does not support."""


@dataclass(frozen=True)
class ProxyFlow:
    flow_ref: str
    target_ref: str
    method: str
    path: str
    request_json: dict[str, Any] | None
    captured_response: CapturedResponse

    def summary(self) -> dict[str, Any]:
        return {
            "flow_ref": self.flow_ref,
            "target_ref": self.target_ref,
            "method": self.method,
            "path": self.path,
            "request_json_fields": sorted(self.request_json or {}),
            "request_body_omitted": self.request_json is not None,
            "captured_response": self.captured_response.summary(),
        }


class ProxyCaptureBackend(Protocol):
    provider: str
    ignored_flow_count: int

    def list_flows(self, target_ref: str | None = None) -> list[ProxyFlow]: ...

    def get_flow(self, flow_ref: str) -> ProxyFlow: ...


class HarCaptureBackend:
    """Read a bounded HAR interchange file exported by a proxy connector.

    Authentication headers, cookies, query strings and raw bodies never cross
    the MCP boundary. Out-of-scope entries are ignored during import.
    """

    provider = "har-v1"

    def __init__(self, flows: list[ProxyFlow], ignored_flow_count: int) -> None:
        self._flows = tuple(flows)
        self._by_ref = {flow.flow_ref: flow for flow in flows}
        self.ignored_flow_count = ignored_flow_count

    @classmethod
    def load(cls, path: str, policy: ServerPolicy) -> HarCaptureBackend:
        capture_path = Path(path)
        try:
            raw_document = capture_path.read_bytes()
        except OSError as exc:
            raise ToolInputError("invalid proxy capture catalog") from exc
        return cls.from_bytes(raw_document, policy)

    @classmethod
    def from_bytes(cls, raw_document: bytes, policy: ServerPolicy) -> HarCaptureBackend:
        try:
            if len(raw_document) > MAX_CAPTURE_BYTES:
                raise ToolInputError("proxy capture exceeds configured file limit")
            document = json.loads(raw_document)
            entries = document["log"]["entries"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ToolInputError("invalid proxy capture catalog") from exc
        if not isinstance(entries, list):
            raise ToolInputError("invalid proxy capture entries")

        flows: list[ProxyFlow] = []
        ignored = 0
        for index, entry in enumerate(entries):
            flow = _parse_har_entry(entry, index, policy)
            if flow is None:
                ignored += 1
                continue
            flows.append(flow)
            if len(flows) > policy.proxy_max_flows:
                raise ToolInputError("proxy capture exceeds configured flow limit")
        return cls(flows, ignored)

    def list_flows(self, target_ref: str | None = None) -> list[ProxyFlow]:
        return [flow for flow in self._flows if target_ref is None or flow.target_ref == target_ref]

    def get_flow(self, flow_ref: str) -> ProxyFlow:
        try:
            return self._by_ref[flow_ref]
        except KeyError as exc:
            raise ToolInputError("unknown flow_ref") from exc


def _parse_har_entry(entry: Any, index: int, policy: ServerPolicy) -> ProxyFlow | None:
    if not isinstance(entry, dict):
        raise ToolInputError("invalid proxy capture entry")
    try:
        request_item = entry["request"]
        response_item = entry["response"]
        method = str(request_item["method"]).upper()
        parsed = urlsplit(str(request_item["url"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolInputError("invalid proxy capture entry") from exc
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None

    target_ref = _match_target(policy, parsed.scheme, parsed.netloc)
    if target_ref is None:
        return None
    profile = policy.targets[target_ref]
    path = parsed.path or "/"
    allowed_methods = profile.allowed_path_methods.get(path, profile.allowed_methods)
    if path not in profile.allowed_paths or method not in allowed_methods:
        return None

    try:
        request_json = _request_json(request_item.get("postData"))
    except _UnsupportedRequestBody:
        return None
    response = _captured_response(response_item, policy.max_response_bytes)
    digest_input = f"{index}\0{target_ref}\0{method}\0{path}".encode()
    flow_ref = f"flow_{hashlib.sha256(digest_input).hexdigest()[:16]}"
    return ProxyFlow(flow_ref, target_ref, method, path, request_json, response)


def _match_target(policy: ServerPolicy, scheme: str, netloc: str) -> str | None:
    for target_ref, profile in policy.targets.items():
        origin = urlsplit(profile.origin)
        if scheme == origin.scheme and netloc == origin.netloc:
            return target_ref
    return None


def _request_json(post_data: Any) -> dict[str, Any] | None:
    if post_data is None:
        return None
    if not isinstance(post_data, dict):
        raise ToolInputError("invalid captured request body")
    mime_type = str(post_data.get("mimeType", "")).split(";", 1)[0].strip().lower()
    text = post_data.get("text")
    if not isinstance(text, str):
        raise ToolInputError("invalid captured request body")
    if mime_type != "application/json":
        raise _UnsupportedRequestBody
    if len(text.encode()) > _MAX_REQUEST_BODY_BYTES:
        raise ToolInputError("captured request body exceeds limit")
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ToolInputError("invalid captured JSON request body") from exc
    if not isinstance(body, dict):
        raise ToolInputError("captured JSON request body must be an object")
    return body


def _captured_response(item: Any, max_bytes: int) -> CapturedResponse:
    if not isinstance(item, dict):
        raise ToolInputError("invalid captured response")
    try:
        status = int(item["status"])
        header_items = item.get("headers", [])
        headers = {
            str(header["name"]).lower(): str(header["value"])
            for header in header_items
            if isinstance(header, dict) and "name" in header and "value" in header
        }
        content = item.get("content", {})
        text = content.get("text", "")
        if not isinstance(text, str):
            raise TypeError
        body = (
            base64.b64decode(text, validate=True)
            if content.get("encoding") == "base64"
            else text.encode()
        )
    except (binascii.Error, KeyError, TypeError, ValueError) as exc:
        raise ToolInputError("invalid captured response") from exc
    if not 100 <= status <= 599 or len(body) > max_bytes:
        raise ToolInputError("captured response exceeds limits")
    return CapturedResponse(status, headers, body)


def _require_replay(policy: ServerPolicy, flow: ProxyFlow) -> None:
    profile = policy.targets[flow.target_ref]
    if not policy.proxy_replay_enabled:
        raise ToolInputError("proxy replay is disabled by operator configuration")
    if flow.path not in profile.replayable_paths:
        raise ToolInputError("captured path is not enabled for replay")


def _require_session_for_write(flow: ProxyFlow, session_ref: str | None) -> None:
    if flow.method not in _READ_METHODS and session_ref is None:
        raise ToolInputError("write replay requires a configured session_ref")


def create_server(policy: ServerPolicy, capture_path: str) -> MCPServer:
    backend: ProxyCaptureBackend = HarCaptureBackend.load(capture_path, policy)
    server = MCPServer("web-proxy", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool()
    def proxy_status() -> dict[str, Any]:
        """Report capture provider and operator replay switch; no target traffic."""
        return {
            "provider": backend.provider,
            "flow_count": len(backend.list_flows()),
            "ignored_out_of_scope_flows": backend.ignored_flow_count,
            "replay_enabled": policy.proxy_replay_enabled,
        }

    @server.tool()
    def proxy_list_flows(target_ref: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List bounded, in-scope flow summaries without credentials or raw bodies."""
        if target_ref is not None and target_ref not in policy.targets:
            raise ToolInputError("unknown target_ref")
        if limit < 1 or limit > 100:
            raise ToolInputError("limit must be between 1 and 100")
        flows = backend.list_flows(target_ref)
        return {
            "flows": [flow.summary() for flow in flows[:limit]],
            "may_be_truncated": len(flows) > limit,
        }

    @server.tool()
    def proxy_get_flow(flow_ref: str) -> dict[str, Any]:
        """Return one redacted capture summary. Header values and bodies remain omitted."""
        return backend.get_flow(flow_ref).summary()

    @server.tool()
    async def proxy_reload_capture() -> dict[str, Any]:
        """Atomically reload the operator-owned capture file; never contacts a target."""
        nonlocal backend
        replacement = HarCaptureBackend.load(capture_path, policy)
        async with execution_lock:
            backend = replacement
        return {
            "provider": backend.provider,
            "flow_count": len(backend.list_flows()),
            "ignored_out_of_scope_flows": backend.ignored_flow_count,
            "reloaded": True,
        }

    @server.tool()
    async def proxy_replay_flow(flow_ref: str, session_ref: str | None = None) -> dict[str, Any]:
        """Replay one operator-approved flow with a configured test session, never captured auth."""
        flow = backend.get_flow(flow_ref)
        _require_replay(policy, flow)
        _require_session_for_write(flow, session_ref)
        async with execution_lock:
            response = await request(
                policy,
                flow.target_ref,
                flow.path,
                method=flow.method,
                session_ref=session_ref,
                json_body=flow.request_json,
            )
        return {"flow_ref": flow_ref, "target_ref": flow.target_ref, **response.summary()}

    @server.tool()
    async def proxy_mutate_json(
        flow_ref: str,
        mutations: dict[str, Any],
        session_ref: str | None = None,
    ) -> dict[str, Any]:
        """Change allowlisted top-level JSON fields, then replay once with a test session."""
        flow = backend.get_flow(flow_ref)
        _require_replay(policy, flow)
        _require_session_for_write(flow, session_ref)
        if flow.request_json is None:
            raise ToolInputError("captured flow has no JSON object body")
        allowed_fields = set(policy.targets[flow.target_ref].mutable_json_fields.get(flow.path, []))
        if not mutations or set(mutations) - allowed_fields:
            raise ToolInputError("JSON mutation field is not enabled for this path")
        body = {**flow.request_json, **mutations}
        async with execution_lock:
            response = await request(
                policy,
                flow.target_ref,
                flow.path,
                method=flow.method,
                session_ref=session_ref,
                json_body=body,
            )
        return {
            "flow_ref": flow_ref,
            "target_ref": flow.target_ref,
            "mutated_fields": sorted(mutations),
            **response.summary(),
        }

    @server.tool()
    async def proxy_compare_sessions(
        flow_ref: str,
        first_session_ref: str,
        second_session_ref: str,
    ) -> dict[str, Any]:
        """Replay one read-only flow with two distinct configured test principals."""
        flow = backend.get_flow(flow_ref)
        _require_replay(policy, flow)
        if flow.method not in _READ_METHODS or flow.request_json is not None:
            raise ToolInputError("session comparison requires a bodyless read request")
        first = policy.sessions.get(first_session_ref)
        second = policy.sessions.get(second_session_ref)
        if (
            first is None
            or second is None
            or first.target_ref != flow.target_ref
            or second.target_ref != flow.target_ref
            or first.principal_ref == second.principal_ref
        ):
            raise ToolInputError("two distinct principals bound to this target are required")
        async with execution_lock:
            a = await request(policy, flow.target_ref, flow.path, session_ref=first_session_ref)
            b = await request(policy, flow.target_ref, flow.path, session_ref=second_session_ref)
        return {
            "flow_ref": flow_ref,
            "first": {"principal_ref": first.principal_ref, **a.summary()},
            "second": {"principal_ref": second.principal_ref, **b.summary()},
            "same_status": a.status == b.status,
            "same_body": a.body == b.body,
            "verdict": "observation_only",
            "request_count": 2,
        }

    return server
