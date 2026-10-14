"""Low-impact reconnaissance. DNS/TLS/HTTP probes still generate target traffic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from typing import Any

from mcp.server import MCPServer

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.inspection import html_observations
from redteam.capability.servers.network import ToolInputError, request, resolve_target


def object_document(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ToolInputError("expected a bounded JSON object document") from exc
    if not isinstance(document, dict):
        raise ToolInputError("expected a JSON object document")
    return document


def create_server(policy: ServerPolicy) -> MCPServer:
    server = MCPServer("recon-passive", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool()
    async def dns_resolve(target_ref: str) -> dict[str, Any]:
        """Resolve a configured host and reject any address outside approved networks."""
        async with execution_lock:
            target = await resolve_target(policy, target_ref)
        return {"target_ref": target_ref, "addresses": list(target.addresses)}

    @server.tool()
    async def http_probe(target_ref: str, path: str = "/") -> dict[str, Any]:
        """Single GET for reachability and security-header presence, without redirects."""
        async with execution_lock:
            response = await request(policy, target_ref, path)
        return {"target_ref": target_ref, **response.summary()}

    @server.tool()
    async def tls_inspect(target_ref: str) -> dict[str, Any]:
        """One verified TLS handshake: negotiated protocol, cipher and certificate digest.

        Does not enumerate all ciphers or disable certificate validation.
        """
        async with execution_lock:
            target = await resolve_target(policy, target_ref)
            if not target.profile.origin.startswith("https://"):
                raise ToolInputError("TLS inspection requires a configured HTTPS origin")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        target.addresses[0],
                        target.port,
                        ssl=ssl.create_default_context(),
                        server_hostname=target.host,
                    ),
                    timeout=policy.timeout_seconds,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                raise ToolInputError("TLS connection or certificate verification failed") from exc
            try:
                tls = writer.get_extra_info("ssl_object")
                certificate = tls.getpeercert(binary_form=True)
                return {
                    "target_ref": target_ref,
                    "host": target.host,
                    "port": target.port,
                    "scheme": "https",
                    "service": "tls",
                    "verified": True,
                    "protocol": tls.version(),
                    "cipher": tls.cipher()[0],
                    "certificate_sha256": hashlib.sha256(certificate).hexdigest(),
                }
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), policy.timeout_seconds)
                except (OSError, asyncio.TimeoutError):
                    pass

    @server.tool()
    async def openapi_inventory(target_ref: str, path: str) -> dict[str, Any]:
        """Fetch one approved OpenAPI JSON path. List operations; never follow $ref or servers.

        Reported paths are candidates, not automatically authorized targets or confirmed findings.
        """
        async with execution_lock:
            response = await request(policy, target_ref, path)
        if response.status != 200:
            raise ToolInputError("OpenAPI document did not return HTTP 200")
        document = object_document(response.body)
        paths = document.get("paths")
        if not isinstance(paths, dict) or not ("openapi" in document or "swagger" in document):
            raise ToolInputError("expected OpenAPI paths and version")
        operations = []
        for route, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method in ("get", "head", "options", "post", "put", "patch", "delete"):
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                operations.append(
                    {
                        "path": route[:512],
                        "method": method.upper(),
                        "declares_security": bool(
                            operation.get("security", document.get("security"))
                        ),
                        "deprecated": operation.get("deprecated") is True,
                    }
                )
                if len(operations) >= 200:
                    break
            if len(operations) >= 200:
                break
        return {
            "target_ref": target_ref,
            "operations": operations,
            "limit": 200,
            "may_be_truncated": len(operations) == 200,
            "external_refs_followed": False,
            "source_untrusted": True,
            "source_sha256": hashlib.sha256(response.body).hexdigest(),
        }

    @server.tool()
    async def oidc_inventory(target_ref: str, path: str) -> dict[str, Any]:
        """Inspect an approved OIDC discovery document without visiting advertised endpoints."""
        async with execution_lock:
            response = await request(policy, target_ref, path)
        if response.status != 200:
            raise ToolInputError("OIDC document did not return HTTP 200")
        document = object_document(response.body)
        if not isinstance(document.get("issuer"), str):
            raise ToolInputError("OIDC issuer is missing")
        list_keys = (
            "response_types_supported",
            "grant_types_supported",
            "code_challenge_methods_supported",
            "token_endpoint_auth_methods_supported",
        )
        capabilities = {}
        for key in list_keys:
            values = document.get(key, [])
            capabilities[key] = (
                [str(v)[:100] for v in values[:30]] if isinstance(values, list) else []
            )
        return {
            "target_ref": target_ref,
            "capabilities": capabilities,
            "endpoints_present": {
                key: isinstance(document.get(key), str)
                for key in (
                    "authorization_endpoint",
                    "token_endpoint",
                    "jwks_uri",
                    "revocation_endpoint",
                )
            },
            "source_untrusted": True,
            "endpoints_followed": False,
            "verdict": "observation_only",
        }

    @server.tool()
    async def html_inventory(target_ref: str, path: str) -> dict[str, Any]:
        """One approved GET: list static same-origin link/form/script paths, never follow them.

        Query/fragment values and form contents are omitted. Discovered paths are candidates,
        not newly authorized targets. Does not identify financial product versions.
        """
        async with execution_lock:
            response = await request(policy, target_ref, path)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if response.status != 200 or media_type != "text/html":
            raise ToolInputError("HTML inventory requires HTTP 200 and text/html")
        return {
            "target_ref": target_ref,
            **response.summary(),
            **html_observations(
                response.body, policy.targets[target_ref], path, response.headers["content-type"],
            ),
            "source_sha256": hashlib.sha256(response.body).hexdigest(),
            "request_count": 1,
        }

    return server
