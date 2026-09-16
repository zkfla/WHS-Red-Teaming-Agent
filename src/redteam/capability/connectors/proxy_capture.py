"""Normalize ZAP API and Burp saved-items exports into the neutral proxy HAR catalog."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError
from redteam.capability.servers.proxy import (
    MAX_CAPTURE_BYTES,
    HarCaptureBackend,
    _parse_har_entry,
)

_SENSITIVE_REQUEST_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_SENSITIVE_RESPONSE_HEADERS = {"set-cookie", "proxy-authenticate"}
_HTTP_STATUS = re.compile(rb"^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s|$)")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_zap_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ToolInputError("ZAP API URL must be an http(s) origin without credentials")
    _ = parsed.port
    return value.rstrip("/")


async def fetch_zap_har(
    api_url: str,
    base_url: str,
    api_key: str,
    max_flows: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 10,
) -> bytes:
    """Fetch one bounded HAR page from ZAP's Import/Export add-on."""
    origin = _validate_zap_origin(api_url)
    headers = {"Accept-Encoding": "identity", "X-ZAP-API-Key": api_key}
    params = {"baseurl": base_url, "start": "0", "count": str(max_flows)}
    try:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
        ) as client:
            async with client.stream(
                "GET",
                f"{origin}/OTHER/exim/other/exportHar/",
                params=params,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise ToolInputError("ZAP HAR export failed")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ToolInputError("compressed ZAP HAR export is not accepted")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CAPTURE_BYTES:
                        raise ToolInputError("ZAP HAR export exceeds configured file limit")
    except (httpx.HTTPError, OSError, asyncio.TimeoutError, TimeoutError) as exc:
        raise ToolInputError("ZAP API request failed") from exc
    return bytes(body)


def _decode_burp_message(node: ET.Element) -> bytes:
    text = node.text or ""
    if node.get("base64", "false").lower() == "true":
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolInputError("invalid Burp saved-items base64") from exc
    return text.encode()


def _split_http_message(raw: bytes) -> tuple[list[bytes], bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        head, separator, body = raw.partition(b"\n\n")
    if not separator:
        raise ToolInputError("invalid HTTP message in Burp saved items")
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    if not lines or not lines[0]:
        raise ToolInputError("invalid HTTP message in Burp saved items")
    return lines, body


def _headers(lines: list[bytes]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for line in lines:
        name, separator, value = line.partition(b":")
        if not separator:
            raise ToolInputError("invalid HTTP header in Burp saved items")
        result.append(
            {
                "name": name.decode("latin-1").strip(),
                "value": value.decode("latin-1").strip(),
            }
        )
    return result


def convert_burp_xml(raw_xml: bytes) -> bytes:
    """Convert Burp's saved-items XML format without executing or following any request."""
    if len(raw_xml) > MAX_CAPTURE_BYTES:
        raise ToolInputError("Burp saved-items file exceeds configured file limit")
    lowered = raw_xml.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ToolInputError("Burp saved-items XML declarations are not accepted")
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise ToolInputError("invalid Burp saved-items XML") from exc

    entries: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        url = item.findtext("url")
        request_node = item.find("request")
        response_node = item.find("response")
        if not url or request_node is None or response_node is None:
            continue
        request_lines, request_body = _split_http_message(_decode_burp_message(request_node))
        response_lines, response_body = _split_http_message(_decode_burp_message(response_node))
        request_parts = request_lines[0].split(b" ")
        status_match = _HTTP_STATUS.match(response_lines[0])
        if len(request_parts) < 3 or status_match is None:
            raise ToolInputError("invalid HTTP start line in Burp saved items")
        request_headers = _headers(request_lines[1:])
        response_headers = _headers(response_lines[1:])
        request: dict[str, Any] = {
            "method": request_parts[0].decode("ascii"),
            "url": url,
            "headers": request_headers,
        }
        if request_body:
            content_type = next(
                (
                    header["value"]
                    for header in request_headers
                    if header["name"].lower() == "content-type"
                ),
                "application/octet-stream",
            )
            try:
                request_text = request_body.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                request_text = ""
                content_type = "application/octet-stream"
            request["postData"] = {"mimeType": content_type, "text": request_text}
        entries.append(
            {
                "request": request,
                "response": {
                    "status": int(status_match.group(1)),
                    "headers": response_headers,
                    "content": {
                        "text": base64.b64encode(response_body).decode(),
                        "encoding": "base64",
                    },
                },
            }
        )
    return json.dumps({"log": {"version": "1.2", "entries": entries}}).encode()


def normalize_capture(
    raw_har: bytes,
    policy: ServerPolicy,
    target_ref: str | None = None,
) -> bytes:
    """Keep only supported in-scope flows and remove captured authentication material."""
    if len(raw_har) > MAX_CAPTURE_BYTES:
        raise ToolInputError("proxy capture exceeds configured file limit")
    try:
        document = json.loads(raw_har)
        log = document["log"]
        entries = log["entries"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ToolInputError("invalid proxy capture catalog") from exc
    if not isinstance(log, dict) or not isinstance(entries, list):
        raise ToolInputError("invalid proxy capture catalog")

    accepted: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        flow = _parse_har_entry(entry, index, policy)
        if flow is None or (target_ref is not None and flow.target_ref != target_ref):
            continue
        request_item = entry["request"]
        response_item = entry["response"]
        request_item["headers"] = _without_headers(
            request_item.get("headers", []), _SENSITIVE_REQUEST_HEADERS
        )
        response_item["headers"] = _without_headers(
            response_item.get("headers", []), _SENSITIVE_RESPONSE_HEADERS
        )
        request_item.pop("cookies", None)
        response_item.pop("cookies", None)
        accepted.append(entry)

    log["entries"] = accepted
    normalized = json.dumps(document, separators=(",", ":")).encode()
    HarCaptureBackend.from_bytes(normalized, policy)
    return normalized


def _without_headers(items: Any, denied: set[str]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ToolInputError("invalid proxy capture headers")
    return [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("name", "")).lower() not in denied
    ]


def write_capture_atomic(
    raw_har: bytes,
    output: Path,
    policy: ServerPolicy,
    target_ref: str | None = None,
) -> int:
    normalized = normalize_capture(raw_har, policy, target_ref)
    flow_count = len(HarCaptureBackend.from_bytes(normalized, policy).list_flows())
    if flow_count == 0:
        raise ToolInputError("connector produced no supported in-scope flows")
    if output.is_symlink():
        raise ToolInputError("capture output must not be a symbolic link")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_mode = stat.S_IMODE(output.stat().st_mode)
    except FileNotFoundError:
        output_mode = 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), output_mode)
            stream.write(normalized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return flow_count


async def _run(args: argparse.Namespace) -> int:
    policy = ServerPolicy.load(args.policy)
    if args.target_ref not in policy.targets:
        raise ToolInputError("unknown target_ref")
    if args.source == "zap":
        if not _ENVIRONMENT_NAME.fullmatch(args.api_key_env):
            raise ToolInputError("invalid ZAP API key environment variable name")
        api_key = os.environ.get(args.api_key_env)
        if not api_key or any(character in api_key for character in "\r\n\x00"):
            raise ToolInputError("ZAP API credential unavailable")
        raw_har = await fetch_zap_har(
            args.api_url,
            policy.targets[args.target_ref].origin,
            api_key,
            policy.proxy_max_flows,
        )
    else:
        try:
            raw_xml = Path(args.input).read_bytes()
        except OSError as exc:
            raise ToolInputError("unable to read Burp saved-items file") from exc
        raw_har = convert_burp_xml(raw_xml)
    count = write_capture_atomic(raw_har, Path(args.output), policy, args.target_ref)
    print(json.dumps({"capture_path": str(Path(args.output)), "flow_count": count}))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a bounded neutral proxy capture")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--target-ref", required=True)
    parser.add_argument("--output", required=True)
    subparsers = parser.add_subparsers(dest="source", required=True)
    zap = subparsers.add_parser("zap")
    zap.add_argument("--api-url", required=True)
    zap.add_argument("--api-key-env", required=True)
    burp = subparsers.add_parser("burp")
    burp.add_argument("--input", required=True)
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
