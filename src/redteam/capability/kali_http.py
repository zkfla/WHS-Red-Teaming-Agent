"""Canonical web-http provider executed inside the selected Kali workroom.

The public MCP identity and schemas remain ``web.web-http.*``.  Only the
execution substrate changes: a provider-owned stdlib client runs through the
same supervisor and direct-target network window as Kali programs.  Its JSON
envelope is an internal provider protocol, not a mapper for arbitrary stdout.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.adapters.mcp.streamable_http import apply_provider_metadata
from redteam.capability.adapters.mcp.transport import remote_tool_metadata
from redteam.capability.direct_target import DirectTargetExecutionPolicy, DirectTargetProviderConfig
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import (
    CapturedResponse,
    ToolInputError,
    session_header,
    validate_path,
)
from redteam.capability.servers.web_http import create_server as create_legacy_web_http_server
from redteam.capability.shell_workroom import (
    ShellWorkroomError,
    ShellWorkroomManager,
    workroom_key_for,
)
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.target import UrlTarget
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ToolDescriptor,
    ToolError,
    ToolInvocation,
    ToolResourceEffect,
    ToolResult,
    ToolTargetBinding,
)
from redteam.core.convert import digest_payload, execution_input_digest
from redteam.core.enums import EvidenceSourceType, RiskLevel, ToolHealthStatus
from redteam.core.ids import EvidenceId, new_id
from redteam.core.ports.evidence_store import EvidenceStore

SERVER_ID = "web-http"
RECEIPT_VERSION = 1
_RECOVERY_RECORD_SOURCE = "capability.kali_http"
_RECOVERY_RECORD_VERSION = 1
_RECOVERY_TOOL_NAMES = frozenset({
    "http_request", "http_compare_sessions", "http_compare_anonymous", "http_security_posture",
})


# The request envelope, including any credential, is stdin.  The script and
# argv contain no request values.  Response bytes exist only in the protected,
# bounded supervisor pipe until this adapter computes its redacted projection;
# discard_provider_record removes that pipe immediately afterwards.
_CLIENT_SOURCE = r"""
import base64, hashlib, http.client, json, socket, ssl, sys
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urljoin, urlsplit

def emit(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")))

def failure(code, phase, *, received=False, status=None, body=b"", headers=None,
            http_version=None, truncated=False):
    emit({"ok": False, "diagnostic": {"code": code, "phase": phase,
          "response_received": received, "partial_response": received and bool(body)},
          "status": status, "body_b64": base64.b64encode(body).decode("ascii"),
          "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body),
          "body_complete": False, "body_truncated": truncated,
          "safe_headers": safe_headers(headers or [], request, status),
          "http_version": http_version})

def safe_headers(pairs, request, status=None):
    grouped = {}
    for name, value in pairs:
        grouped.setdefault(name.lower(), []).append(value)
    present = {name: name in grouped for name in (
        "strict-transport-security", "content-security-policy", "x-content-type-options",
        "cache-control")}
    posture = {name: name in grouped for name in (
        "strict-transport-security", "content-security-policy",
        "content-security-policy-report-only", "x-frame-options", "x-content-type-options",
        "referrer-policy", "permissions-policy")}
    content_type = grouped.get("content-type", [""])[0].split(";", 1)[0].lower()[:128]
    cache = {item.strip().split("=", 1)[0].lower()
             for value in grouped.get("cache-control", []) for item in value.split(",")}
    cookies = []
    for raw in grouped.get("set-cookie", [])[:50]:
        cookie = SimpleCookie()
        try: cookie.load(raw)
        except CookieError: cookie.clear()
        if len(cookie) != 1:
            cookies.append({"parse_status": "unknown"}); continue
        morsel = next(iter(cookie.values())); same = morsel["samesite"].lower()
        cookies.append({"parse_status": "parsed", "secure": bool(morsel["secure"]),
            "http_only": bool(morsel["httponly"]),
            "same_site": same if same in {"strict","lax","none"} else
                         "unspecified" if not same else "invalid",
            "domain_attribute_present": bool(morsel["domain"]),
            "persistent": bool(morsel["expires"] or morsel["max-age"])})
    result = {"names": sorted(grouped), "content_type": content_type,
        "security_headers_present": present, "posture_headers_present": posture,
        "cache_control_no_store": "no-store" in cache,
        "content_type_nosniff": grouped.get("x-content-type-options", [""])[0].lower()=="nosniff",
        "cookies": cookies, "set_cookie_header_count": len(grouped.get("set-cookie", [])),
        "cookies_truncated": len(grouped.get("set-cookie", [])) > 50,
        "values_omitted": True}
    locations = grouped.get("location", [])
    if locations and isinstance(status, int) and 300 <= status < 400:
        result["redirect"] = {"detected": True, "decision": "not_followed"}
        try:
            location = urlsplit(urljoin(request["origin"] + request["path"], locations[0]))
            expected = urlsplit(request["origin"])
            same_origin = (location.scheme, location.hostname, location.port or
                (443 if location.scheme == "https" else 80)) == (
                expected.scheme, expected.hostname, expected.port or
                (443 if expected.scheme == "https" else 80))
            path = location.path or "/"
            allowed = request["allow_all_paths"] or path in request["allowed_paths"]
            result["redirect"].update({"same_origin": same_origin,
                "in_scope": same_origin and allowed, "location_path": path[:512],
                "query_omitted": bool(location.query), "fragment_omitted": bool(location.fragment)})
        except Exception:
            result["redirect"].update({"same_origin": False, "in_scope": False})
    challenges = grouped.get("www-authenticate", [])
    if challenges:
        scheme = challenges[0].split(None, 1)[0][:32]
        result["auth_challenge"] = {"present": True, "scheme": scheme,
            "realm_present": "realm=" in challenges[0].lower()}
    return result

class PinnedHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)

class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)

request = json.load(sys.stdin)
body = base64.b64decode(request["body_b64"], validate=True)
cls = PinnedHTTPS if request["scheme"] == "https" else PinnedHTTP
kwargs = {"host": request["host"], "port": request["port"], "timeout": request["timeout"]}
if cls is PinnedHTTPS: kwargs["context"] = ssl.create_default_context()
conn = cls(**kwargs); conn._pinned_ip = request["connect_ip"]
response = None; captured = bytearray(); pairs = []; phase = "connect"
try:
    conn.connect(); phase = "request"
    conn.putrequest(request["method"], request["request_target"],
                    skip_host=True, skip_accept_encoding=True)
    for name, value in request["headers"].items(): conn.putheader(name, value)
    conn.endheaders(body if body else None); phase = "response"
    response = conn.getresponse(); pairs = response.getheaders()
    if any(name.lower() == "content-encoding" and value.lower() != "identity"
           for name, value in pairs):
        failure("target_http_compressed_response", "response", received=True,
                status=response.status, headers=pairs); raise SystemExit
    while True:
        try:
            chunk = response.read(min(65536, request["max_response_bytes"] + 1 - len(captured)))
        except http.client.IncompleteRead as exc:
            for partial in (exc.partial, getattr(exc.__cause__, "partial", b"")):
                if isinstance(partial, bytes):
                    remaining = request["max_response_bytes"] + 1 - len(captured)
                    captured.extend(partial[:max(0, remaining)])
            too_large = len(captured) > request["max_response_bytes"]
            failure("target_http_response_too_large" if too_large else
                    "target_http_incomplete_response", "response", received=True,
                    status=response.status,
                    body=bytes(captured[:request["max_response_bytes"]]), headers=pairs,
                    http_version=response.version, truncated=too_large); raise SystemExit
        if not chunk:
            if response.length is not None and response.length > 0:
                failure("target_http_incomplete_response", "response", received=True,
                        status=response.status, body=bytes(captured), headers=pairs,
                        http_version=response.version); raise SystemExit
            break
        captured.extend(chunk)
        if len(captured) > request["max_response_bytes"]:
            failure("target_http_response_too_large", "response", received=True,
                    status=response.status, body=bytes(captured[:request["max_response_bytes"]]),
                    headers=pairs, http_version=response.version, truncated=True); raise SystemExit
    emit({"ok": True, "status": response.status,
          "body_b64": base64.b64encode(captured).decode("ascii"),
          "body_sha256": hashlib.sha256(captured).hexdigest(), "body_bytes": len(captured),
          "body_complete": True, "body_truncated": False,
          "safe_headers": safe_headers(pairs, request, response.status),
          "http_version": response.version})
except SystemExit: raise
except ssl.SSLError:
    failure("target_http_tls_failed", "tls", received=False)
except socket.timeout:
    code = {"connect": "target_http_connect_timeout",
            "request": "target_http_request_timeout",
            "response": "target_http_response_timeout"}[phase]
    failure(code, phase, received=response is not None,
            status=response.status if response else None, body=bytes(captured), headers=pairs,
            http_version=response.version if response else None)
except ConnectionRefusedError:
    failure("target_http_connection_refused", "connect", received=False)
except OSError:
    code = ("target_http_partial_response" if response is not None else
            "target_http_connect_failed" if phase == "connect" else "target_http_request_failed")
    failure(code, phase, received=response is not None,
            status=response.status if response else None, body=bytes(captured), headers=pairs,
            http_version=response.version if response else None)
except Exception:
    failure("target_http_protocol_failed", "request_or_response", received=response is not None,
            status=response.status if response else None, body=bytes(captured), headers=pairs,
            http_version=response.version if response else None)
finally:
    try: conn.close()
    except Exception: pass
"""


class _RequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_ref: str
    path: str
    method: str = "GET"
    session_ref: str | None = None
    json_body: dict[str, Any] | None = None
    form_body: dict[str, str] | None = None
    query: dict[str, str] | None = None
    request_headers: dict[str, str] | None = None


def _descriptor(
    name: str,
    description: str,
    schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    operation: str,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=build_canonical_mcp_tool_id("web", SERVER_ID, name),
        server_id=SERVER_ID,
        tool_name=name,
        description=description,
        input_schema=schema,
        output_schema=output_schema,
        operations=[operation],
        target_types=["host", "domain", "url", "endpoint", "service"],
        capability_tags=["web"],
        risk_level=RiskLevel.HIGH,
        retry_safe=False,
        target_binding=ToolTargetBinding(binds_target_arguments=True),
        requires_target=name != "session_list",
        execution_route="target" if name != "session_list" else None,
        availability_impact=False,
    )


class BuiltinKaliHttpAdapter:
    """Provider-owned canonical HTTP receipts over one Kali workroom route."""

    server_id = SERVER_ID

    def __init__(
        self,
        workroom: ShellWorkroomManager,
        executor: DirectTargetExecutionPolicy,
        provider: DirectTargetProviderConfig,
        policy: ServerPolicy,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._workroom, self._executor, self._provider, self._policy = (
            workroom,
            executor,
            provider,
            policy,
        )
        self._evidence_store = evidence_store
        self._ordinal_admission_lock = asyncio.Lock()
        self._resource_effects_by_tool: dict[str, list[ToolResourceEffect]] | None = None
        for _ref, profile in policy.targets.items():
            parsed = urlsplit(profile.origin)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if (
                host not in provider.network.targets
                or port not in provider.network.target_ports[host]
            ):
                raise ValueError("kali_http_route_profile_mismatch")

    async def discover_tools(self) -> list[ToolDescriptor]:
        # Keep the public schema/description owner in the existing web-http
        # server.  Listing these local definitions performs no target request
        # and prevents the in-process transport replacement from drifting.
        public = {
            item.name: item
            for item in await create_legacy_web_http_server(self._policy).list_tools()
        }
        operations = {
            "session_list": "response_analysis",
            "http_request": "http_request",
            "http_compare_sessions": "response_analysis",
            "http_security_posture": "response_analysis",
            "http_compare_anonymous": "response_analysis",
        }
        descriptors: list[ToolDescriptor] = []
        resource_effects_by_tool: dict[str, list[ToolResourceEffect]] = {}
        for name, operation in operations.items():
            public_tool = public[name]
            descriptor = _descriptor(
                name,
                public_tool.description or "Canonical scoped HTTP operation.",
                dict(public_tool.input_schema),
                dict(public_tool.output_schema or {}),
                operation,
            )
            apply_provider_metadata(descriptor, remote_tool_metadata(public_tool))
            descriptors.append(descriptor)
            if descriptor.resource_effects is not None:
                resource_effects_by_tool[str(descriptor.tool_id)] = descriptor.resource_effects
        self._resource_effects_by_tool = resource_effects_by_tool
        return descriptors

    async def health_check(self) -> ToolHealthStatus:
        try:
            alive = await self._workroom.probe()
        except Exception:
            return ToolHealthStatus.UNKNOWN
        return ToolHealthStatus.HEALTHY if alive else ToolHealthStatus.UNHEALTHY

    def prepare_reference(self, invocation: ToolInvocation) -> ExecutionReference | None:
        """Derive stable per-ordinal recovery selectors without provider I/O."""
        if self._evidence_store is None:
            return None
        try:
            prefix = f"web.{SERVER_ID}."
            tool_id = str(invocation.tool_id)
            if not tool_id.startswith(prefix):
                return None
            tool_name = tool_id[len(prefix):]
            if tool_name not in _RECOVERY_TOOL_NAMES:
                return None
            context = invocation.execution_context
            if context.instance_id is None:
                return None
            input_digest = execution_input_digest(invocation)
            workroom_key = workroom_key_for(
                str(invocation.run_id), str(context.instance_id)
            )
            ordinal_count = 2 if tool_name in {
                "http_compare_sessions", "http_compare_anonymous",
            } else 1
            tags = [
                self._supervisor_tag(invocation, input_digest, ordinal)
                for ordinal in range(ordinal_count)
            ]
            return ExecutionReference(
                engagement_id=context.engagement_id,
                run_id=invocation.run_id,
                instance_id=context.instance_id,
                action_id=invocation.action_id,
                invocation_id=invocation.invocation_id,
                tool_id=invocation.tool_id,
                input_digest=input_digest,
                provider_handle={
                    "version": _RECOVERY_RECORD_VERSION,
                    "tool": tool_name,
                    "workroom_key": workroom_key,
                    "ordinal_tags": tags,
                },
            )
        except Exception:
            return None

    @staticmethod
    def _supervisor_tag(invocation: ToolInvocation, input_digest: str, ordinal: int) -> str:
        identity = "\0".join((
            str(invocation.run_id), str(invocation.action_id),
            str(invocation.invocation_id), input_digest, str(ordinal),
        ))
        return "khttp_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24] + (
            "_" + str(ordinal)
        )

    @staticmethod
    def _reference_parts(reference: ExecutionReference) -> tuple[str, list[str]] | None:
        try:
            prefix = f"web.{SERVER_ID}."
            tool_id = str(reference.tool_id)
            if not tool_id.startswith(prefix) or reference.instance_id is None:
                return None
            tool_name = tool_id[len(prefix):]
            if tool_name not in _RECOVERY_TOOL_NAMES:
                return None
            workroom_key = workroom_key_for(
                str(reference.run_id), str(reference.instance_id)
            )
            ordinal_count = 2 if tool_name in {
                "http_compare_sessions", "http_compare_anonymous",
            } else 1
            tags = []
            for ordinal in range(ordinal_count):
                identity = "\0".join((
                    str(reference.run_id), str(reference.action_id),
                    str(reference.invocation_id), reference.input_digest, str(ordinal),
                ))
                tags.append(
                    "khttp_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                    + "_" + str(ordinal)
                )
            expected = {
                "version": _RECOVERY_RECORD_VERSION,
                "tool": tool_name,
                "workroom_key": workroom_key,
                "ordinal_tags": tags,
            }
            if reference.provider_handle != expected:
                return None
            return workroom_key, tags
        except Exception:
            return None

    @staticmethod
    def _reference_digest(reference: ExecutionReference) -> str:
        canonical = json.dumps(
            reference.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def query_execution(
        self, reference: ExecutionReference
    ) -> ExecutionRecoveryAnswer:
        """Prove only provider termination; never promote partial output to a result."""
        held = ExecutionRecoveryAnswer(
            state=ExecutionState.UNKNOWN,
            verified=False,
            uncertain=True,
            result_available=False,
        )
        if self._evidence_store is None:
            return held
        parts = self._reference_parts(reference)
        if parts is None:
            return held
        workroom_key, tags = parts
        try:
            entered = await self._ordinal_entries(reference)
            if entered is None:
                return held
            for ordinal, tag in enumerate(tags):
                has_started, has_receipt = entered.get(ordinal, (False, False))
                if has_receipt and not has_started:
                    return held
                record = await self._workroom.supervisor_record(workroom_key, tag)
                if not has_started:
                    if record is not None:
                        return held
                    outcome, alive = await self._workroom.supervisor_outcome(
                        workroom_key, tag
                    )
                    if outcome is not None or alive:
                        return held
                    continue
                if not isinstance(record, dict) or not ShellWorkroomManager.record_trusted(record):
                    return held
                if record.get("outcome") not in {"completed", "timeout", "cancelled"}:
                    return held
                outcome, alive = await self._workroom.supervisor_outcome(
                    workroom_key, tag
                )
                if outcome != record.get("outcome") or alive:
                    return held
            if not await self._route_is_closed(workroom_key):
                return held
        except Exception:
            return held
        return ExecutionRecoveryAnswer(
            state=ExecutionState.UNKNOWN,
            verified=True,
            uncertain=True,
            result_available=False,
            execution_terminated=True,
            error=ToolError(
                code="kali_http_original_result_unavailable",
                message=(
                    "HTTP execution termination is verified; the complete result is unavailable"
                ),
            ),
        )

    async def fetch_execution(self, reference: ExecutionReference) -> ToolResult | None:
        # Ordinal projections deliberately are not reassembled as a successful
        # composite response. The normal executor owns the final ToolResult.
        return None

    async def _ordinal_entries(
        self, reference: ExecutionReference,
    ) -> dict[int, tuple[bool, bool]] | None:
        if self._evidence_store is None:
            return None
        reference_digest = self._reference_digest(reference)
        states: dict[int, tuple[bool, bool]] = {}
        event_counts: dict[int, tuple[int, int]] = {}
        for record in await self._evidence_store.list_for_run(reference.run_id):
            if (
                record.source_type is not EvidenceSourceType.SYSTEM
                or record.source_id != _RECOVERY_RECORD_SOURCE
                or record.invocation_id != reference.invocation_id
            ):
                continue
            payload = record.metadata
            if (
                record.run_id != reference.run_id
                or record.engagement_id != reference.engagement_id
                or record.action_id != reference.action_id
            ):
                return None
            if (
                payload.get("record_kind") not in {
                    "kali_http_ordinal_started", "kali_http_ordinal_receipt",
                }
                or payload.get("schema_version") != _RECOVERY_RECORD_VERSION
                or payload.get("invocation_id") != str(reference.invocation_id)
                or payload.get("action_id") != str(reference.action_id)
                or payload.get("tool_id") != str(reference.tool_id)
                or payload.get("input_digest") != reference.input_digest
                or payload.get("reference_digest") != reference_digest
                or record.digest != digest_payload(payload)
            ):
                return None
            ordinal = payload.get("ordinal")
            tags = reference.provider_handle.get("ordinal_tags")
            if (
                type(ordinal) is not int or not isinstance(tags, list)
                or not 0 <= ordinal < len(tags) or payload.get("tag") != tags[ordinal]
            ):
                return None
            was_started, has_receipt = states.get(ordinal, (False, False))
            if payload.get("record_kind") == "kali_http_ordinal_started":
                started_count, receipt_count = event_counts.get(ordinal, (0, 0))
                if started_count:
                    return None
                event_counts[ordinal] = (1, receipt_count)
                states[ordinal] = (True, has_receipt)
            else:
                started_count, receipt_count = event_counts.get(ordinal, (0, 0))
                if receipt_count:
                    return None
                event_counts[ordinal] = (started_count, 1)
                safe = payload.get("safe_result")
                if not isinstance(safe, dict) or not self._safe_result_matches_reference(
                    safe, reference, ordinal
                ):
                    return None
                states[ordinal] = (was_started, True)
        return states

    @staticmethod
    def _safe_result_matches_reference(
        safe: dict[str, Any], reference: ExecutionReference, ordinal: int,
    ) -> bool:
        receipt = safe.get("http_receipt")
        if not isinstance(receipt, dict):
            return False
        if set(safe) - {"ordinal", "success", "http_receipt", "diagnostic_code"}:
            return False
        if set(receipt) != {"version", "provider", "request", "response"}:
            return False
        request = receipt.get("request")
        response = receipt.get("response")
        body = response.get("body") if isinstance(response, dict) else None
        if not isinstance(request, dict) or not isinstance(response, dict):
            return False
        if set(request) - {
            "binding", "invocation_id", "action_id", "input_digest", "method", "target_ref",
            "canonical_target", "path", "query_sha256", "query_keys", "allowed_header_names",
            "body_sha256", "body_bytes", "session_ref", "principal_ref", "principal_scope",
            "redirect_policy",
        }:
            return False
        if set(response) - {
            "request_binding", "response_received", "status", "safe_headers", "body", "peer",
            "transport", "failure_phase",
        }:
            return False
        if not isinstance(body, dict) or set(body) - {
            "artifact_ref", "sha256", "size_bytes", "truncated", "retention",
        }:
            return False
        headers = response.get("safe_headers")
        if isinstance(headers, dict) and set(headers) - {
            "names", "content_type", "security_headers_present", "posture_headers_present",
            "cache_control_no_store", "content_type_nosniff", "cookies",
            "set_cookie_header_count", "cookies_truncated", "values_omitted", "redirect",
            "auth_challenge",
        }:
            return False
        return (
            safe.get("ordinal") == ordinal
            and type(safe.get("success")) is bool
            and receipt.get("version") == RECEIPT_VERSION
            and receipt.get("provider") == "kali-http-v1"
            and request.get("invocation_id") == str(reference.invocation_id)
            and request.get("action_id") == str(reference.action_id)
            and request.get("input_digest") == reference.input_digest
            and response.get("request_binding") == request.get("binding")
            and body.get("retention") == "digest_only"
            and isinstance(body.get("sha256"), str)
            and type(body.get("size_bytes")) is int
        )

    def _provider_output_limit(self) -> int:
        return min(
            2 * 1024 * 1024,
            max(
                self._workroom.policy.max_output_bytes,
                (self._policy.max_response_bytes * 4 // 3) + 131072,
            ),
        )

    @staticmethod
    def _safe_ordinal_projection(
        result: MCPCallResult, ordinal: int, reference: ExecutionReference,
    ) -> dict[str, Any] | None:
        raw_output = result.output if isinstance(result.output, dict) else {}
        raw_receipt = raw_output.get("http_receipt")
        if not isinstance(raw_receipt, dict):
            return None
        request = raw_receipt.get("request")
        response = raw_receipt.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            return None
        request_fields = (
            "binding", "invocation_id", "action_id", "input_digest", "method", "target_ref",
            "canonical_target", "path", "query_sha256", "query_keys", "allowed_header_names",
            "body_sha256", "body_bytes", "session_ref", "principal_ref", "principal_scope",
            "redirect_policy",
        )
        response_fields = (
            "request_binding", "response_received", "status", "safe_headers", "body", "peer",
            "transport", "failure_phase",
        )
        safe_request = {key: request[key] for key in request_fields if key in request}
        safe_response = {key: response[key] for key in response_fields if key in response}
        safe_headers = safe_response.get("safe_headers")
        if isinstance(safe_headers, dict):
            header_fields = (
                "names", "content_type", "security_headers_present", "posture_headers_present",
                "cache_control_no_store", "content_type_nosniff", "cookies",
                "set_cookie_header_count", "cookies_truncated", "values_omitted", "redirect",
                "auth_challenge",
            )
            safe_response["safe_headers"] = {
                key: safe_headers[key] for key in header_fields if key in safe_headers
            }
        raw_body = safe_response.get("body")
        if isinstance(raw_body, dict):
            safe_response["body"] = {
                key: raw_body[key]
                for key in ("artifact_ref", "sha256", "size_bytes", "truncated", "retention")
                if key in raw_body
            }
        safe_receipt = {
            "version": raw_receipt.get("version"),
            "provider": raw_receipt.get("provider"),
            "request": safe_request,
            "response": safe_response,
        }
        safe_result: dict[str, Any] = {
            "ordinal": ordinal,
            "success": result.success,
            "http_receipt": safe_receipt,
        }
        error_code = result.error.code if result.error is not None else None
        if isinstance(error_code, str) and len(error_code) <= 128:
            safe_result["diagnostic_code"] = error_code
        if not BuiltinKaliHttpAdapter._safe_result_matches_reference(
            safe_result, reference, ordinal
        ):
            return None
        try:
            json.dumps(safe_result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return None
        return safe_result

    async def _route_is_closed(self, workroom_key: str) -> bool:
        extension = getattr(self._executor, "extension", None)
        inventory_mode = getattr(extension, "inventory_mode", None)
        expected_networks = getattr(extension, "expected_networks", None)
        if not callable(inventory_mode) or not callable(expected_networks):
            return False
        if inventory_mode(workroom_key) != "full":
            return False
        if expected_networks(workroom_key) != frozenset({"none"}):
            return False
        container_state = await self._workroom.workroom_container_state(workroom_key)
        if container_state == "gone":
            return True
        if container_state not in {"running", "stopped"}:
            return False
        return await self._workroom.verify_workroom_ownership(workroom_key)

    async def _append_ordinal_marker(
        self, reference: ExecutionReference, ordinal: int, *, receipt: dict[str, Any] | None = None,
    ) -> None:
        if self._evidence_store is None:
            return
        entries = await self._ordinal_entries(reference)
        if entries is None:
            raise ShellWorkroomError("kali_http_recovery_store_unreadable")
        started, has_receipt = entries.get(ordinal, (False, False))
        if receipt is None:
            if started or has_receipt:
                raise ShellWorkroomError("kali_http_ordinal_replay_refused", uncertainty=True)
            parts = self._reference_parts(reference)
            if parts is None:
                raise ShellWorkroomError("kali_http_recovery_reference_invalid")
            workroom_key, tags = parts
            tag = tags[ordinal]
            prior = await self._workroom.supervisor_record(workroom_key, tag)
            outcome, alive = await self._workroom.supervisor_outcome(workroom_key, tag)
            if prior is not None or outcome is not None or alive:
                raise ShellWorkroomError("kali_http_ordinal_replay_refused", uncertainty=True)
        elif not started or has_receipt:
            raise ShellWorkroomError("kali_http_ordinal_receipt_state_invalid", uncertainty=True)
        payload: dict[str, Any] = {
            "record_kind": (
                "kali_http_ordinal_receipt" if receipt is not None
                else "kali_http_ordinal_started"
            ),
            "schema_version": _RECOVERY_RECORD_VERSION,
            "reference_digest": self._reference_digest(reference),
            "invocation_id": str(reference.invocation_id),
            "action_id": str(reference.action_id),
            "tool_id": str(reference.tool_id),
            "input_digest": reference.input_digest,
            "ordinal": ordinal,
            "tag": reference.provider_handle["ordinal_tags"][ordinal],
        }
        if receipt is not None:
            payload["safe_result"] = receipt
        record = EvidenceRecord(
            evidence_id=EvidenceId(new_id("evidence")),
            run_id=reference.run_id,
            engagement_id=reference.engagement_id,
            source_type=EvidenceSourceType.SYSTEM,
            source_id=_RECOVERY_RECORD_SOURCE,
            action_id=reference.action_id,
            invocation_id=reference.invocation_id,
            content_type="application/json",
            digest=digest_payload(payload),
            summary=(
                f"Kali HTTP ordinal {ordinal} safe receipt"
                if receipt is not None else f"Kali HTTP ordinal {ordinal} entered"
            ),
            created_at=datetime.now(timezone.utc),
            metadata=payload,
        )
        await self._evidence_store.append(record)

    async def _capture_safe_ordinal_from_spool(
        self,
        invocation: ToolInvocation,
        reference: ExecutionReference,
        args: _RequestArgs,
        *,
        started: datetime,
        ordinal: int,
        tag: str,
        payload: dict[str, Any],
        body: bytes,
        host: str,
        port: int,
        principal: str | None,
        request_binding: str,
    ) -> bool:
        """Project a complete protected envelope, then leave only safe fields."""
        if self._evidence_store is None:
            return False
        try:
            workroom_key = workroom_key_for(
                str(invocation.run_id), str(invocation.execution_context.instance_id)
            )
            record = await self._workroom.supervisor_record(
                workroom_key, tag,
            )
            if (
                not isinstance(record, dict)
                or not ShellWorkroomManager.record_trusted(record)
                or record.get("outcome") not in {"completed", "timeout", "cancelled"}
                or record.get("spool") != "complete"
            ):
                return False
            workroom_key = workroom_key_for(
                str(invocation.run_id), str(invocation.execution_context.instance_id)
            )
            meta = await self._workroom.spool_record(workroom_key, tag)
            if (
                not isinstance(meta, dict)
                or meta.get("sealed") is not True
                or meta.get("invocation_id") != str(invocation.invocation_id)
                or meta.get("input_digest") != reference.input_digest
            ):
                return False
            expected_digest = record.get("spool_digest")
            if not isinstance(expected_digest, str):
                return False
            canonical = json.dumps(meta, sort_keys=True).encode("utf-8")
            if hashlib.sha256(canonical).hexdigest() != expected_digest:
                return False
            for stream in ("out", "err"):
                entry = meta.get(stream)
                if (
                    not isinstance(entry, dict)
                    or entry.get("complete") is not True
                    or entry.get("truncated") is not False
                    or type(entry.get("size")) is not int
                    or entry["size"] < 0
                    or entry["size"] > self._provider_output_limit()
                    or not isinstance(entry.get("sha256"), str)
                ):
                    return False
            output_limit = self._provider_output_limit()
            raw_out = await self._workroom.read_spool_stream(
                workroom_key, tag, stream="out", limit_bytes=output_limit
            )
            raw_err = await self._workroom.read_spool_stream(
                workroom_key, tag, stream="err", limit_bytes=output_limit
            )
            if raw_out is None or raw_err is None:
                return False
            for raw, stream in ((raw_out, "out"), (raw_err, "err")):
                entry = meta[stream]
                if (
                    len(raw) != entry["size"]
                    or hashlib.sha256(raw).hexdigest() != entry["sha256"]
                ):
                    return False
            try:
                raw_envelope = json.loads(raw_out.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return False
            result = self._result_from_envelope(
                raw_envelope, invocation, args, body, host, port, principal,
                request_binding, started,
            )
            safe = self._safe_ordinal_projection(result, ordinal, reference)
            if safe is None:
                return False
            await self._append_ordinal_marker(reference, ordinal, receipt=safe)
            return True
        except Exception:
            return False

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        return self._failure("kali_http_context_required", "approved invocation context required")

    def _failure(self, code: str, message: str, *, output=None, started=None, metadata=None):
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=False,
            started_at=started or now,
            finished_at=now,
            output=output,
            error=ToolError(code=code, message=message, retryable=False),
            metadata=metadata or {},
        )

    def _workroom_failure(
        self,
        exc: ShellWorkroomError,
        name: str,
        started: datetime,
        *,
        output: dict[str, Any] | None = None,
        execution_started: bool = False,
        detail_metadata: dict[str, Any] | None = None,
    ) -> MCPCallResult:
        metadata: dict[str, Any] = {
            "tool_name": name,
            "failure_phase": "execution",
            EXECUTION_UNCERTAIN_METADATA_KEY: exc.uncertainty,
        }
        if execution_started:
            metadata[EXECUTION_STARTED_METADATA_KEY] = True
        if detail_metadata:
            metadata.update(detail_metadata)
        return self._failure(
            self._workroom_failure_code(exc),
            "Kali HTTP workroom rejected the request",
            output=output,
            started=started,
            metadata=metadata,
        )

    @staticmethod
    def _workroom_failure_code(exc: ShellWorkroomError) -> str:
        code = getattr(exc, "reason_code", None)
        return code if isinstance(code, str) and code else "kali_http_workroom_failed"

    @staticmethod
    def _partial_comparison_output(
        target_ref: str,
        first_output: Any,
        second_output: Any,
        failed_code: str,
        *,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        first = first_output if isinstance(first_output, dict) else {}
        second = second_output if isinstance(second_output, dict) else {}
        second_diagnostic = second.get("diagnostic")
        if not isinstance(second_diagnostic, dict):
            second_diagnostic = {"code": failed_code, "phase": "execution"}
        result: dict[str, Any] = {
            "target_ref": target_ref,
            "partial_comparison": {
                "first": {
                    "actual_session_ref": first.get("actual_session_ref"),
                    "principal_ref": first.get("principal_ref"),
                    "http_receipt": first.get("http_receipt"),
                },
                "second": {
                    "diagnostic": second_diagnostic,
                    "http_receipt": second.get("http_receipt"),
                },
                "completed_request_count": 1,
                "failed_request_ordinal": 1,
            },
            "diagnostic": {
                "code": "kali_http_partial_session_comparison",
                "phase": "second_request",
                "failed_request_code": failed_code,
            },
        }
        if outcome is not None:
            result["partial_comparison"]["outcome"] = outcome
            result["diagnostic"]["outcome"] = outcome
        return result

    async def invoke_contextual(self, invocation: ToolInvocation) -> MCPCallResult:
        resource_effects_by_tool = self._resource_effects_by_tool
        if resource_effects_by_tool is None:
            await self.discover_tools()
            resource_effects_by_tool = self._resource_effects_by_tool
        assert resource_effects_by_tool is not None
        result = await self._invoke_contextual(invocation)
        resource_effects = resource_effects_by_tool.get(str(invocation.tool_id))
        if resource_effects is not None:
            result.metadata["resource_effects"] = [
                effect.model_dump(mode="json") for effect in resource_effects
            ]
        return result

    async def _invoke_contextual(self, invocation: ToolInvocation) -> MCPCallResult:
        started = datetime.now(timezone.utc)
        name = str(invocation.tool_id).split(".")[-1]
        try:
            if name == "session_list":
                return MCPCallResult(
                    success=True,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    output={
                        "sessions": [
                            {
                                "session_ref": ref,
                                "target_ref": item.target_ref,
                                "principal_ref": item.principal_ref,
                            }
                            for ref, item in self._policy.sessions.items()
                        ]
                    },
                    metadata={"tool_name": name, "receipt_provider": "kali-http-v1"},
                )
            if name == "http_request":
                return await self._one(
                    invocation,
                    _RequestArgs.model_validate(invocation.arguments),
                    started=started,
                    ordinal=0,
                )
            if name == "http_security_posture":
                args = _RequestArgs.model_validate({**invocation.arguments, "method": "GET"})
                result = await self._one(invocation, args, started=started, ordinal=0)
                if result.success and isinstance(result.output, dict):
                    safe = (
                        result.output.get("http_receipt", {})
                        .get("response", {})
                        .get("safe_headers", {})
                    )
                    result.output.update(
                        {
                            "headers_present": safe.get("posture_headers_present", {}),
                            "cache_control_no_store": safe.get("cache_control_no_store") is True,
                            "content_type_nosniff": safe.get("content_type_nosniff") is True,
                            "cookies": safe.get("cookies", []),
                            "set_cookie_header_count": safe.get("set_cookie_header_count", 0),
                            "cookies_truncated": safe.get("cookies_truncated") is True,
                            "raw_headers_omitted": True,
                            "cookie_names_and_values_omitted": True,
                            "verdict": "observation_only",
                            "request_count": 1,
                        }
                    )
                return result
            if name in {"http_compare_sessions", "http_compare_anonymous"}:
                return await self._compare(invocation, name, started)
            return self._failure(
                "kali_http_unknown_tool", "unknown canonical HTTP tool", started=started
            )
        except ShellWorkroomError as exc:
            return self._workroom_failure(exc, name, started)
        except (ValidationError, ToolInputError, ValueError) as exc:
            code = str(exc) if isinstance(exc, ToolInputError) else "kali_http_invalid_arguments"
            return self._failure(
                code,
                "canonical HTTP request rejected",
                started=started,
                metadata={"tool_name": name, "failure_phase": "validation"},
            )
        except Exception:
            return self._failure(
                "kali_http_internal_error",
                "canonical HTTP request failed",
                started=started,
                metadata={"tool_name": name, "failure_phase": "provider"},
            )

    def _prepare(self, args: _RequestArgs) -> tuple[dict[str, Any], bytes, str, int, str | None]:
        profile = self._policy.targets.get(args.target_ref)
        if profile is None:
            raise ToolInputError("unknown target_ref")
        validate_path(profile, args.path)
        allowed = profile.allowed_path_methods.get(args.path, profile.allowed_methods)
        if args.method not in allowed:
            raise ToolInputError("HTTP method is not enabled for this target")
        if args.json_body is not None and args.form_body is not None:
            raise ToolInputError("select one request body format")
        if (args.json_body is not None or args.form_body is not None) and (
            args.method in {"GET", "HEAD", "OPTIONS"} or not profile.request_body_enabled
        ):
            raise ToolInputError("body is not allowed for read requests")
        body = b""
        if args.json_body is not None:
            body = json.dumps(args.json_body, separators=(",", ":")).encode()
        elif args.form_body is not None:
            body = urlencode(args.form_body).encode()
        if len(body) > 65536:
            raise ToolInputError("request body exceeds limit")
        query = args.query or {}
        if len(query) > 20 or any(
            not profile.allows_query_key(args.path, key)
            or len(value) > 1024
            or any(ord(char) < 32 for char in value)
            for key, value in query.items()
        ):
            raise ToolInputError("query outside configured allowlist or size limit")
        supplied = args.request_headers or {}
        headers = {key.lower(): value for key, value in supplied.items()}
        if (
            len(headers) != len(supplied)
            or len(headers) > 20
            or any(
                not profile.allows_request_header(key)
                or len(value) > 1024
                or any(ord(char) < 32 or ord(char) > 126 for char in value)
                for key, value in headers.items()
            )
        ):
            raise ToolInputError("request headers outside configured allowlist or size limit")
        credential = session_header(self._policy, args.target_ref, args.session_ref, args.path)
        headers.update(credential)
        parsed = urlsplit(profile.origin)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        headers.update(
            {"Host": f"{host}:{port}", "Accept-Encoding": "identity", "Connection": "close"}
        )
        if body:
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded"
                if args.form_body is not None
                else "application/json"
            )
            headers["Content-Length"] = str(len(body))
        principal = (
            self._policy.sessions[args.session_ref].principal_ref
            if args.session_ref is not None
            else None
        )
        payload = {
            "origin": profile.origin,
            "scheme": parsed.scheme,
            "host": host,
            "port": port,
            "connect_ip": self._provider.network.targets[host][0],
            "method": args.method,
            "path": args.path,
            "request_target": args.path + (("?" + urlencode(query)) if query else ""),
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "timeout": self._policy.timeout_seconds,
            "max_response_bytes": self._policy.max_response_bytes,
            "allowed_paths": profile.allowed_paths,
            "allow_all_paths": profile.allow_all_paths,
        }
        return payload, body, host, port, principal

    def _validate_approved_url_scheme(
        self, invocation: ToolInvocation, args: _RequestArgs
    ) -> None:
        approved = invocation.execution_context.approved_target
        if not isinstance(approved, UrlTarget):
            return
        profile = self._policy.targets.get(args.target_ref)
        if profile is None:
            return
        approved_scheme = urlsplit(approved.url).scheme.lower()
        configured_scheme = urlsplit(profile.origin).scheme.lower()
        if approved_scheme != configured_scheme:
            raise ToolInputError("kali_http_url_scheme_mismatch")

    async def _one(
        self, invocation: ToolInvocation, args: _RequestArgs, *, started: datetime, ordinal: int
    ) -> MCPCallResult:
        self._validate_approved_url_scheme(invocation, args)
        payload, body, host, port, principal = self._prepare(args)
        input_digest = execution_input_digest(invocation)
        reference = self.prepare_reference(invocation) if self._evidence_store is not None else None
        tags = reference.provider_handle.get("ordinal_tags") if reference is not None else None
        if reference is not None and (
            not isinstance(tags, list) or ordinal >= len(tags) or not isinstance(tags[ordinal], str)
        ):
            raise ShellWorkroomError("kali_http_recovery_reference_invalid")
        tag = tags[ordinal] if isinstance(tags, list) else self._supervisor_tag(
            invocation, input_digest, ordinal
        )
        recoverable = self._evidence_store is not None and reference is not None
        request_binding = hashlib.sha256(
            json.dumps(
                {
                    "invocation_id": str(invocation.invocation_id),
                    "action_id": str(invocation.action_id),
                    "input_digest": input_digest,
                    "ordinal": ordinal,
                    "target_ref": args.target_ref,
                    "method": args.method,
                    "path": args.path,
                    "query": args.query or {},
                    "session_ref": args.session_ref,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = workroom_key_for(
            str(invocation.run_id), str(invocation.execution_context.instance_id)
        )
        entered = False
        receipt_persisted = False
        cancelled = False

        async def persist_safe_result(result: MCPCallResult) -> MCPCallResult:
            nonlocal receipt_persisted
            if recoverable and reference is not None:
                safe = self._safe_ordinal_projection(result, ordinal, reference)
                if safe is None:
                    raise ShellWorkroomError(
                        "kali_http_safe_ordinal_receipt_invalid", uncertainty=True
                    )
                try:
                    await self._append_ordinal_marker(reference, ordinal, receipt=safe)
                except ShellWorkroomError:
                    raise
                except Exception as exc:
                    raise ShellWorkroomError(
                        "kali_http_ordinal_receipt_persist_failed", uncertainty=True
                    ) from exc
                receipt_persisted = True
            return result

        async def cleanup_provider_record() -> None:
            if recoverable:
                discard_streams = getattr(self._workroom, "discard_provider_streams", None)
                if callable(discard_streams):
                    cleaned = await discard_streams(key, tag)
                else:
                    cleaned = await self._workroom.discard_provider_record(key, tag)
                if cleaned is not True:
                    # Raw bytes take precedence over the recovery seal. If the
                    # narrow cleanup fails, remove the full record as a safe
                    # fallback; termination will then remain unverified.
                    cleaned = await self._workroom.discard_provider_record(key, tag)
            else:
                cleaned = await self._workroom.discard_provider_record(key, tag)
            if cleaned is not True:
                raise ShellWorkroomError("kali_http_spool_cleanup_failed", uncertainty=True)

        async def execute():
            nonlocal entered, cancelled
            output_limit = self._provider_output_limit()
            if recoverable and reference is not None:
                try:
                    async with self._ordinal_admission_lock:
                        await self._append_ordinal_marker(reference, ordinal)
                except ShellWorkroomError as exc:
                    return self._workroom_failure(
                        exc,
                        str(invocation.tool_id).split(".")[-1],
                        started,
                        execution_started=exc.uncertainty,
                    )
                except Exception:
                    return self._workroom_failure(
                        ShellWorkroomError("kali_http_recovery_store_unavailable"),
                        str(invocation.tool_id).split(".")[-1],
                        started,
                    )
            entered = True
            try:
                execution = await self._workroom.exec_program(
                    key,
                    "/usr/bin/python3",
                    ["-c", _CLIENT_SOURCE],
                    json.dumps(payload, separators=(",", ":")),
                    timeout_seconds=min(
                        invocation.timeout_seconds, self._policy.timeout_seconds + 2
                    ),
                    exec_tag=tag,
                    invocation_id=str(invocation.invocation_id),
                    input_digest=input_digest,
                    retain_record=True,
                    _provider_input_limit=256 * 1024,
                    _provider_output_limit=output_limit,
                    _provider_argv_limit=64 * 1024,
                )
                if execution.timed_out:
                    return await persist_safe_result(self._transport_failure(
                        "target_http_execution_timeout", "execution", started,
                        request_binding, args, host, port,
                        request_body=body, principal=principal, invocation=invocation,
                        input_digest=input_digest,
                    ))
                if execution.exit_code != 0 or execution.truncated:
                    return await persist_safe_result(self._transport_failure(
                        "kali_http_receipt_unavailable", "execution", started,
                        request_binding, args, host, port,
                        request_body=body, principal=principal, invocation=invocation,
                        input_digest=input_digest,
                    ))
                try:
                    raw = json.loads(execution.stdout)
                except (TypeError, ValueError):
                    return await persist_safe_result(self._transport_failure(
                        "kali_http_receipt_invalid", "protocol", started,
                        request_binding, args, host, port,
                        request_body=body, principal=principal, invocation=invocation,
                        input_digest=input_digest,
                    ))
                result = self._result_from_envelope(
                    raw, invocation, args, body, host, port, principal, request_binding, started
                )
                return await persist_safe_result(result)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                if entered:
                    try:
                        if recoverable and not receipt_persisted and reference is not None:
                            await asyncio.shield(self._capture_safe_ordinal_from_spool(
                                invocation,
                                reference,
                                args,
                                started=started,
                                ordinal=ordinal,
                                tag=tag,
                                payload=payload,
                                body=body,
                                host=host,
                                port=port,
                                principal=principal,
                                request_binding=request_binding,
                            ))
                        await asyncio.shield(cleanup_provider_record())
                    except asyncio.CancelledError:
                        if not cancelled:
                            raise
                    except Exception:
                        if not cancelled:
                            raise ShellWorkroomError(
                                "kali_http_spool_cleanup_failed", uncertainty=True
                            ) from None

        try:
            return await self._executor.execute_route(
                invocation, target_ref=host, port=port, delegate=execute
            )
        except ShellWorkroomError as exc:
            tool_name = str(invocation.tool_id).split(".")[-1]
            if tool_name in {"http_compare_sessions", "http_compare_anonymous"}:
                raise
            return self._workroom_failure(
                exc,
                tool_name,
                started,
                execution_started=entered or exc.uncertainty,
            )

    def _transport_failure(
        self,
        code,
        phase,
        started,
        binding,
        args,
        host,
        port,
        *,
        request_body=b"",
        principal=None,
        invocation=None,
        input_digest=None,
    ):
        receipt = self._receipt(
            args,
            request_body,
            host,
            port,
            principal,
            binding,
            invocation,
            input_digest,
            {
                "response_received": False,
                "status": None,
                "safe_headers": {},
                "body": {
                    "artifact_ref": None,
                    "sha256": None,
                    "size_bytes": 0,
                    "truncated": False,
                    "retention": "none",
                },
                "peer": {"ip": self._provider.network.targets[host][0], "port": port},
                "transport": {
                    "scheme": urlsplit(self._policy.targets[args.target_ref].origin).scheme,
                    "proxy": "disabled",
                },
                "failure_phase": phase,
            },
        )
        return self._failure(
            code,
            "target HTTP request failed",
            output={
                "diagnostic": {"code": code, "phase": phase, "response_received": False},
                "http_receipt": receipt,
            },
            started=started,
            metadata={
                "receipt_provider": "kali-http-v1",
                "failure_phase": phase,
                "program_name": "python3",
                "program_executable": "/usr/bin/python3",
            },
        )

    def _result_from_envelope(
        self, raw, invocation, args, request_body, host, port, principal, binding, started
    ):
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            return self._transport_failure(
                "kali_http_receipt_invalid",
                "protocol",
                started,
                binding,
                args,
                host,
                port,
                request_body=request_body,
                principal=principal,
                invocation=invocation,
                input_digest=execution_input_digest(invocation),
            )
        try:
            response_body = base64.b64decode(raw.get("body_b64", ""), validate=True)
        except (ValueError, TypeError):
            return self._transport_failure(
                "kali_http_receipt_invalid",
                "protocol",
                started,
                binding,
                args,
                host,
                port,
                request_body=request_body,
                principal=principal,
                invocation=invocation,
                input_digest=execution_input_digest(invocation),
            )
        digest = hashlib.sha256(response_body).hexdigest()
        if raw.get("body_sha256") != digest or raw.get("body_bytes") != len(response_body):
            return self._transport_failure(
                "kali_http_receipt_invalid",
                "protocol",
                started,
                binding,
                args,
                host,
                port,
                request_body=request_body,
                principal=principal,
                invocation=invocation,
                input_digest=execution_input_digest(invocation),
            )
        safe = raw.get("safe_headers") if isinstance(raw.get("safe_headers"), dict) else {}
        status = raw.get("status")
        response_received = type(status) is int and 100 <= status <= 599
        if raw["ok"] and not response_received:
            return self._transport_failure(
                "kali_http_receipt_invalid",
                "protocol",
                started,
                binding,
                args,
                host,
                port,
                request_body=request_body,
                principal=principal,
                invocation=invocation,
                input_digest=execution_input_digest(invocation),
            )
        response_receipt = {
            "request_binding": binding,
            "response_received": response_received,
            "status": status if response_received else None,
            "safe_headers": safe,
            "body": {
                "artifact_ref": None,
                "sha256": digest,
                "size_bytes": len(response_body),
                "truncated": raw.get("body_truncated") is True,
                "retention": "digest_only",
            },
            "peer": {"ip": self._provider.network.targets[host][0], "port": port},
            "transport": {
                "scheme": urlsplit(self._policy.targets[args.target_ref].origin).scheme,
                "http_version": str(raw.get("http_version")),
                "proxy": "disabled",
                "dns": "operator_pinned",
            },
            "failure_phase": None if raw["ok"] else raw.get("diagnostic", {}).get("phase"),
        }
        receipt = self._receipt(
            args,
            request_body,
            host,
            port,
            principal,
            binding,
            invocation,
            execution_input_digest(invocation),
            response_receipt,
        )
        if not raw["ok"]:
            diagnostic = raw.get("diagnostic") if isinstance(raw.get("diagnostic"), dict) else {}
            code = (
                diagnostic.get("code")
                if isinstance(diagnostic.get("code"), str)
                else ("target_http_transport_failed")
            )
            return self._failure(
                code,
                "target HTTP request failed",
                output={"diagnostic": diagnostic, "http_receipt": receipt},
                started=started,
                metadata={
                    "receipt_provider": "kali-http-v1",
                    "failure_phase": response_receipt["failure_phase"],
                    "program_name": "python3",
                    "program_executable": "/usr/bin/python3",
                },
            )
        redirect = safe.get("redirect") if isinstance(safe.get("redirect"), dict) else None
        if redirect is not None and redirect.get("in_scope") is not True:
            return self._failure(
                "target_http_redirect_scope_denied",
                "redirect target is outside the approved HTTP profile",
                output={
                    "diagnostic": {
                        "code": "target_http_redirect_scope_denied",
                        "phase": "redirect",
                        "response_received": True,
                    },
                    "http_receipt": receipt,
                },
                started=started,
                metadata={
                    "receipt_provider": "kali-http-v1",
                    "failure_phase": "redirect",
                    "program_name": "python3",
                    "program_executable": "/usr/bin/python3",
                },
            )
        projected_headers = {name: "" for name in safe.get("names", [])}
        if isinstance(safe.get("content_type"), str):
            projected_headers["content-type"] = safe["content_type"]
        captured = CapturedResponse(
            status,
            projected_headers,
            response_body,
            host=host,
            port=port,
            scheme=urlsplit(self._policy.targets[args.target_ref].origin).scheme,
            protocol=str(raw.get("http_version")),
            url=self._policy.targets[args.target_ref].origin + args.path,
            actual_session_ref=args.session_ref,
            has_session_receipt=True,
        )
        summary = captured.summary(
            body_max_chars=self._policy.targets[args.target_ref].response_body_max_chars
        )
        summary["security_headers_present"] = safe.get("security_headers_present", {})
        if redirect:
            summary["redirect_metadata"] = {
                key: redirect[key]
                for key in ("detected", "location_path", "query_omitted", "fragment_omitted")
                if key in redirect
            }
        if isinstance(safe.get("auth_challenge"), dict):
            summary["auth_challenge"] = safe["auth_challenge"]
        output = {
            "target_ref": args.target_ref,
            "session_ref": args.session_ref,
            **summary,
            "principal_ref": principal,
            "principal_scope": "session" if args.session_ref is not None else "anonymous",
            "http_receipt": receipt,
        }
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output=output,
            metadata={
                "tool_name": "http_request",
                "receipt_provider": "kali-http-v1",
                "program_name": "python3",
                "program_executable": "/usr/bin/python3",
            },
        )

    def _receipt(
        self, args, body, host, port, principal, binding, invocation, input_digest, response
    ):
        query = urlencode(args.query or {})
        session = args.session_ref
        return {
            "version": RECEIPT_VERSION,
            "provider": "kali-http-v1",
            "request": {
                "binding": binding,
                "invocation_id": (
                    str(invocation.invocation_id) if invocation is not None else None
                ),
                "action_id": str(invocation.action_id) if invocation is not None else None,
                "input_digest": input_digest,
                "method": args.method,
                "target_ref": args.target_ref,
                "canonical_target": {
                    "scheme": urlsplit(self._policy.targets[args.target_ref].origin).scheme,
                    "host": host,
                    "port": port,
                },
                "path": args.path,
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "query_keys": sorted((args.query or {}).keys()),
                "allowed_header_names": sorted(
                    name.lower() for name in (args.request_headers or {})
                ),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "body_bytes": len(body),
                "session_ref": session,
                "principal_ref": principal,
                "principal_scope": "session" if session is not None else "anonymous",
                "redirect_policy": "manual_no_follow",
            },
            "response": response,
        }

    async def _compare(self, invocation, name, started):
        args = invocation.arguments
        target_ref, path = args.get("target_ref"), args.get("path")
        if name == "http_compare_sessions":
            refs = (args.get("first_session_ref"), args.get("second_session_ref"))
            if any(not isinstance(ref, str) for ref in refs) or refs[0] == refs[1]:
                raise ToolInputError("two distinct principals bound to this target are required")
            sessions = [self._policy.sessions.get(ref) for ref in refs]
            if any(item is None or item.target_ref != target_ref for item in sessions) or (
                sessions[0].principal_ref == sessions[1].principal_ref
            ):
                raise ToolInputError("two distinct principals bound to this target are required")
            for ref in refs:
                session_header(self._policy, target_ref, ref, path)
            first = await self._one(
                invocation,
                _RequestArgs(target_ref=target_ref, path=path, session_ref=refs[0]),
                started=started,
                ordinal=0,
            )
            if not first.success:
                return first
            try:
                second = await self._one(
                    invocation,
                    _RequestArgs(target_ref=target_ref, path=path, session_ref=refs[1]),
                    started=started,
                    ordinal=1,
                )
            except ShellWorkroomError as exc:
                code = self._workroom_failure_code(exc)
                output = self._partial_comparison_output(
                    target_ref,
                    first.output,
                    None,
                    code,
                    outcome="unknown" if exc.uncertainty else "failed",
                )
                return self._workroom_failure(
                    exc,
                    name,
                    started,
                    output=output,
                    execution_started=True,
                    detail_metadata={"partial_comparison": True},
                )
            if not second.success:
                failed_code = (
                    second.error.code if second.error is not None
                    else "kali_http_second_request_failed"
                )
                return MCPCallResult(
                    success=False,
                    started_at=started,
                    finished_at=second.finished_at,
                    output=self._partial_comparison_output(
                        target_ref, first.output, second.output, failed_code
                    ),
                    error=second.error or ToolError(
                        code=failed_code,
                        message="second session request failed",
                        retryable=False,
                    ),
                    metadata={
                        **dict(second.metadata or {}),
                        EXECUTION_STARTED_METADATA_KEY: True,
                        "tool_name": name,
                        "partial_comparison": True,
                    },
                )
            a, b = dict(first.output), dict(second.output)
            a["principal_ref"], b["principal_ref"] = (
                sessions[0].principal_ref,
                sessions[1].principal_ref,
            )
            return MCPCallResult(
                success=True,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                output={
                    "target_ref": target_ref,
                    "first": a,
                    "second": b,
                    "same_status": a["status_code"] == b["status_code"],
                    "same_body": a["body_sha256"] == b["body_sha256"],
                    "verdict": "observation_only",
                    "request_count": 2,
                },
                metadata={
                    "tool_name": name,
                    "receipt_provider": "kali-http-v1",
                    "program_name": "python3",
                    "program_executable": "/usr/bin/python3",
                },
            )
        session_ref = args.get("session_ref")
        if not isinstance(session_ref, str) or session_ref not in self._policy.sessions:
            raise ToolInputError("registered session_ref is required")
        # Credential preflight happens before the anonymous request.
        session_header(self._policy, target_ref, session_ref, path)
        anonymous = await self._one(
            invocation, _RequestArgs(target_ref=target_ref, path=path), started=started, ordinal=0
        )
        if not anonymous.success:
            return anonymous
        authenticated = await self._one(
            invocation,
            _RequestArgs(target_ref=target_ref, path=path, session_ref=session_ref),
            started=started,
            ordinal=1,
        )
        if not authenticated.success:
            return authenticated
        a, b = dict(anonymous.output), dict(authenticated.output)
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output={
                "target_ref": target_ref,
                "anonymous": a,
                "authenticated": b,
                "same_status": a["status_code"] == b["status_code"],
                "same_body": a["body_sha256"] == b["body_sha256"],
                "verdict": "observation_only",
                "request_count": 2,
            },
            metadata={
                "tool_name": name,
                "receipt_provider": "kali-http-v1",
                "program_name": "python3",
                "program_executable": "/usr/bin/python3",
            },
        )
