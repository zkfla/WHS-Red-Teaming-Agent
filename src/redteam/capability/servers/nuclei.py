"""Nuclei passive HTTP matching on a single response fetched through the scoped client."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from mcp.server import MCPServer

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import (
    CapturedResponse,
    ToolInputError,
    request,
    resolve_target,
    validate_path,
)

# Reviewed local definitions: no arbitrary templates, paths, DSL, payloads or extractors.
CHECKS = {
    "missing-csp": "content-security-policy:",
    "missing-nosniff-header": "x-content-type-options:",
}

ACTIVE_CHECKS = {"reflected-query-marker"}
ACTIVE_MARKER = "redteam_capability_reflection_probe_7f3a"


async def _run_engine(binary: str, directory: Path, timeout: float) -> bytes:
    args = [
        binary, "-passive", "-target", str(directory / "response.txt"),
        "-t", str(directory / "check.yaml"), "-config", str(directory / "config.yaml"),
        "-jsonl", "-silent", "-nc", "-omit-raw", "-omit-template", "-duc", "-ni",
        "-c", "1", "-bulk-size", "1", "-rate-limit", "1",
    ]
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    for source in ("PUBLIC", "GITHUB", "GITLAB", "AWS", "AZURE"):
        env[f"DISABLE_NUCLEI_TEMPLATES_{source}_DOWNLOAD"] = "true"
    try:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=directory, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolInputError("Nuclei executable unavailable") from exc

    async def collect() -> bytes:
        assert process.stdout is not None
        output = bytearray()
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            if len(output) > 262144:
                raise ToolInputError("Nuclei output exceeds limit")
        await process.wait()
        if process.returncode != 0:
            raise ToolInputError("Nuclei passive analysis failed")
        return bytes(output)

    try:
        return await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError as exc:
        raise ToolInputError("Nuclei execution timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def analyze(binary: str, response: CapturedResponse, check_id: str, timeout: float) -> bool:
    template = {
        "id": check_id,
        "info": {"name": check_id, "author": "capability-team", "severity": "info"},
        "http": [{
            "method": "GET", "path": ["{{BaseURL}}"],
            "matchers": [{
                "type": "word", "part": "header", "words": [CHECKS[check_id]],
                "case-insensitive": True, "negative": True,
            }],
        }],
    }
    # Only header names are needed for these checks: never persist credential values or body.
    header_names = "".join(
        f"{name}: redacted\r\n" for name in response.headers
        if name not in {"content-length", "transfer-encoding", "content-encoding"}
    )
    raw = f"HTTP/1.1 {response.status} Response\r\n{header_names}Content-Length: 0\r\n\r\n"
    with tempfile.TemporaryDirectory(prefix="redteam-nuclei-") as tmp:
        directory = Path(tmp)
        (directory / "response.txt").write_text(raw, encoding="utf-8")
        (directory / "check.yaml").write_text(yaml.safe_dump(template), encoding="utf-8")
        (directory / "config.yaml").write_text("{}", encoding="utf-8")
        output = await _run_engine(binary, directory, timeout)
    matched = False
    try:
        for line in output.splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("template-id") != check_id:
                raise ValueError
            matched = True
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ToolInputError("invalid Nuclei result") from exc
    return matched


async def _run_active_engine(
    binary: str,
    directory: Path,
    *,
    target_url: str,
    host_header: str,
    sni_hostname: str | None,
    timeout: float,
) -> bytes:
    """Run one generated HTTP template against one already validated address."""

    args = [
        binary,
        "-u", target_url,
        "-t", str(directory / "active-check.yaml"),
        "-config", str(directory / "config.yaml"),
        "-H", f"Host: {host_header}",
        "-jsonl", "-silent", "-nc", "-omit-raw", "-omit-template",
        "-duc", "-ni", "-dr", "-retries", "0",
        "-c", "1", "-bulk-size", "1", "-rate-limit", "1",
    ]
    if sni_hostname is not None:
        args.extend(("-sni", sni_hostname))
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    for source in ("PUBLIC", "GITHUB", "GITLAB", "AWS", "AZURE"):
        env[f"DISABLE_NUCLEI_TEMPLATES_{source}_DOWNLOAD"] = "true"
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=directory,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolInputError("Nuclei executable unavailable") from exc

    async def collect() -> bytes:
        assert process.stdout is not None
        output = bytearray()
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            if len(output) > 262144:
                raise ToolInputError("Nuclei output exceeds limit")
        await process.wait()
        if process.returncode != 0:
            raise ToolInputError("Nuclei active analysis failed")
        return bytes(output)

    try:
        return await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError as exc:
        raise ToolInputError("Nuclei execution timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def active_query_probe(
    binary: str,
    policy: ServerPolicy,
    target_ref: str,
    path: str,
    query_key: str,
    check_id: str,
) -> bool:
    """Execute one reviewed template with DNS/IP pinning and no redirect/OAST."""

    if check_id not in ACTIVE_CHECKS:
        raise ToolInputError("Nuclei active check is not allowlisted")
    if not policy.active_enabled:
        raise ToolInputError("active checks are disabled by operator policy")
    profile = policy.targets.get(target_ref)
    if profile is None:
        raise ToolInputError("unknown target_ref")
    validate_path(profile, path)
    if not profile.allows_query_key(path, query_key):
        raise ToolInputError("query key outside configured allowlist")

    target = await resolve_target(policy, target_ref)
    parsed = urlsplit(profile.origin)
    address = target.addresses[0]
    address_host = f"[{address}]" if ":" in address else address
    netloc = f"{address_host}:{target.port}"
    target_url = urlunsplit((parsed.scheme, netloc, path, "", ""))
    host_header = parsed.netloc
    sni_hostname = target.host if parsed.scheme == "https" else None
    template = {
        "id": "bounded-reflected-query-marker",
        "info": {
            "name": "Bounded reflected query marker",
            "author": "capability-team",
            "severity": "info",
        },
        "http": [{
            "method": "GET",
            "path": [f"{{{{BaseURL}}}}?{query_key}={ACTIVE_MARKER}"],
            "matchers": [{"type": "word", "part": "body", "words": [ACTIVE_MARKER]}],
        }],
    }
    with tempfile.TemporaryDirectory(prefix="redteam-nuclei-active-") as tmp:
        directory = Path(tmp)
        (directory / "active-check.yaml").write_text(
            yaml.safe_dump(template), encoding="utf-8"
        )
        (directory / "config.yaml").write_text("{}", encoding="utf-8")
        output = await _run_active_engine(
            binary,
            directory,
            target_url=target_url,
            host_header=host_header,
            sni_hostname=sni_hostname,
            timeout=policy.timeout_seconds + 10,
        )

    matched = False
    try:
        for line in output.splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("template-id") != template["id"]:
                raise ValueError
            matched = True
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ToolInputError("invalid Nuclei result") from exc
    return matched


def create_server(policy: ServerPolicy, binary: str = "nuclei") -> MCPServer:
    server = MCPServer("web-nuclei", log_level="WARNING")
    lock = asyncio.Lock()

    @server.tool()
    async def nuclei_response_check(
        target_ref: str, path: str, check_id: str, session_ref: str | None = None,
    ) -> dict[str, Any]:
        """One scoped GET plus a local Nuclei passive check. No direct scanner target access.

        check_id: missing-csp or missing-nosniff-header. Matches are observations, not findings.
        Does not execute CVE templates, exploits, headless/code templates or active scans.
        """
        if check_id not in CHECKS:
            raise ToolInputError("Nuclei check is not allowlisted")
        executable = shutil.which(binary)
        if executable is None:
            raise ToolInputError("Nuclei executable unavailable")
        async with lock:
            response = await request(policy, target_ref, path, session_ref=session_ref)
            matched = await analyze(executable, response, check_id, policy.timeout_seconds + 10)
        return {
            "target_ref": target_ref, "check_id": check_id, "matched": matched,
            "status_code": response.status, "request_count": 1,
            "engine_mode": "passive", "raw_output_omitted": True,
            "verdict": "observation_only",
        }

    @server.tool()
    async def nuclei_active_query_probe(
        target_ref: str,
        path: str,
        query_key: str,
        check_id: str = "reflected-query-marker",
    ) -> dict[str, Any]:
        """One bounded active GET using a generated, reviewed Nuclei template.

        The target, exact path and query key must be operator-allowlisted and
        active_enabled must be true. Redirects, retries, OAST, code/headless
        templates and template downloads are disabled. A match only means the
        marker was reflected; it does not prove XSS or another vulnerability.
        """

        executable = shutil.which(binary)
        if executable is None:
            raise ToolInputError("Nuclei executable unavailable")
        async with lock:
            matched = await active_query_probe(
                executable, policy, target_ref, path, query_key, check_id
            )
        return {
            "target_ref": target_ref,
            "path": path,
            "query_key": query_key,
            "check_id": check_id,
            "matched": matched,
            "request_count": 1,
            "engine_mode": "active_bounded",
            "raw_output_omitted": True,
            "verdict": "observation_only",
        }

    return server
