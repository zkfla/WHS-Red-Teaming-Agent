"""Operator-scoped, bounded MobSF static-analysis MCP; four REST routes only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import secrets
import sqlite3
import stat
import time
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from redteam.capability.servers.analysis import AnalysisServerPolicy, _resolve

ROUTES = frozenset({"upload", "scan", "scan_logs", "report_json"})
SECTIONS = ("manifest", "permission", "secret", "network")
TOOLS = (
    "mobsf_submit_apk",
    "mobsf_start_scan",
    "mobsf_scan_status",
    "mobsf_report_summary",
)
_SHA = re.compile(r"^[a-f0-9]{64}$")
_MD5 = re.compile(r"^[a-f0-9]{32}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


class MobSFError(ValueError):
    """Fixed error codes only; upstream bodies and credentials never escape."""


class MobSFPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    endpoint: str
    api_key_file: str
    image: str
    workspace_root: str
    analysis: AnalysisServerPolicy
    hashes: dict[str, str]
    engagement_id: str
    run_id: str
    state_path: str
    max_apk_bytes: int = Field(default=100_000_000, ge=1024, le=200_000_000)
    max_response_bytes: int = Field(default=8_388_608, ge=1024, le=33_554_432)
    max_output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    timeout_seconds: float = Field(default=300, gt=0, le=600)
    poll_interval_seconds: float = Field(default=1, gt=0, le=10)
    max_polls: int = Field(default=30, ge=1, le=120)
    max_scans: int = Field(default=16, ge=1, le=100)
    retention_seconds: int = Field(default=3600, ge=60, le=86400)

    @model_validator(mode="after")
    def validate_bindings(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.path
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("mobsf_endpoint_must_be_explicit_loopback_origin")
        if (
            not re.fullmatch(
                r"opensecurity/mobile-security-framework-mobsf:[a-zA-Z0-9._-]+@sha256:[a-f0-9]{64}",
                self.image,
            )
            or ":latest@" in self.image
        ):
            raise ValueError("mobsf_release_and_digest_required")
        if not all(_IDENTITY.fullmatch(v) for v in (self.engagement_id, self.run_id)):
            raise ValueError("mobsf_invalid_context")
        if set(self.hashes) != set(self.analysis.apks) or not all(
            _IDENTITY.fullmatch(k) and _SHA.fullmatch(v) for k, v in self.hashes.items()
        ):
            raise ValueError("mobsf_approved_hashes_required")
        root = Path(self.workspace_root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("mobsf_workspace_required")
        for profile in self.analysis.apks.values():
            try:
                Path(profile.path).absolute().relative_to(root.absolute())
            except ValueError:
                raise ValueError("mobsf_apk_outside_workspace") from None
        return self

    @classmethod
    def load(cls, path: str, expected_sha256: str) -> MobSFPolicy:
        content = Path(path).read_bytes()
        if (
            not _SHA.fullmatch(expected_sha256)
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise MobSFError("mobsf_policy_digest_mismatch")
        return cls.model_validate_json(content)


class MobSFOutput(BaseModel):
    success: bool = True
    apk_ref: str
    scan_ref: str
    sha256: str
    status: Literal["uploaded", "running", "completed", "failed", "timeout", "cancelled"]
    image: str
    sections: dict = Field(default_factory=dict)
    missing_sections: list[str] = Field(default_factory=list)
    truncated: bool = False


class MobSFClient:
    def __init__(self, policy: MobSFPolicy, *, transport: httpx.AsyncBaseTransport | None = None):
        self.policy = policy
        self.transport = transport
        self.lock = asyncio.Lock()
        # Records persist across MCP restarts; the operator chooses a private state file.
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS scans (ref TEXT PRIMARY KEY, engagement TEXT, "
                "run TEXT, apk TEXT, sha TEXT, md5 TEXT, status TEXT, expires REAL)"
            )

    def _db(self):
        return sqlite3.connect(self.policy.state_path, timeout=5)

    def _key(self) -> str:
        try:
            key = Path(self.policy.api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            raise MobSFError("mobsf_api_key_unavailable") from None
        if not key or len(key) > 256 or any(c.isspace() for c in key):
            raise MobSFError("mobsf_api_key_unavailable")
        return key

    def _context(self, engagement_id: str, run_id: str, apk_ref: str) -> None:
        if (engagement_id, run_id) != (self.policy.engagement_id, self.policy.run_id):
            raise MobSFError("mobsf_scope_denied")
        if not self.policy.enabled or apk_ref not in self.policy.hashes:
            raise MobSFError("mobsf_scope_denied")

    def _apk(self, apk_ref: str, expected_sha256: str) -> bytes:
        if expected_sha256 != self.policy.hashes[apk_ref]:
            raise MobSFError("mobsf_hash_mismatch")
        # Reuse Track A's operator registry resolution; never accept a model path.
        path = _resolve(self.policy.analysis, apk_ref).absolute()
        root = Path(self.policy.workspace_root).absolute()
        try:
            path.relative_to(root)
            for part in (path, *path.parents):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise MobSFError("mobsf_unsafe_apk")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise MobSFError("mobsf_unsafe_apk")
            if not path.resolve().is_relative_to(root.resolve()):
                raise MobSFError("mobsf_unsafe_apk")
            with path.open("rb") as stream:
                data = stream.read(self.policy.max_apk_bytes + 1)
            if len(data) > self.policy.max_apk_bytes:
                raise MobSFError("mobsf_apk_too_large")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise MobSFError("mobsf_hash_mismatch")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "AndroidManifest.xml" not in archive.namelist():
                    raise MobSFError("mobsf_invalid_apk")
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            if isinstance(exc, MobSFError):
                raise
            raise MobSFError("mobsf_unsafe_apk") from None
        return data

    async def _request(self, route: str, *, probe: bool = False, **kwargs) -> tuple[int, dict]:
        if route not in ROUTES or (probe and route != "upload"):
            raise MobSFError("mobsf_route_denied")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=3 if probe else self.policy.timeout_seconds,
            ) as client:
                async with client.stream(
                    "GET" if probe else "POST",
                    f"{self.policy.endpoint}/api/v1/{route}",
                    headers={"Authorization": self._key()},
                    **kwargs,
                ) as response:
                    if probe:
                        return response.status_code, {}
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.policy.max_response_bytes:
                            raise MobSFError("mobsf_response_too_large")
                    if response.status_code == 404 and route == "report_json":
                        return 404, {}
                    if response.status_code != 200:
                        raise MobSFError(f"mobsf_{route}_failed")
                    try:
                        payload = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise MobSFError("mobsf_schema_drift") from None
                    if not isinstance(payload, dict):
                        raise MobSFError("mobsf_schema_drift")
                    if "error" in payload:
                        raise MobSFError(f"mobsf_{route}_failed")
                    return response.status_code, payload
        except httpx.TimeoutException:
            raise MobSFError("mobsf_timeout") from None
        except httpx.HTTPError:
            raise MobSFError("mobsf_unavailable") from None

    async def healthy(self) -> bool:
        if not self.policy.enabled:
            return False
        try:
            code, _ = await self._request("upload", probe=True)
            return code == 405
        except MobSFError:
            return False

    def _record(self, apk_ref: str, scan_ref: str, engagement_id: str, run_id: str) -> tuple:
        self._context(engagement_id, run_id, apk_ref)
        with self._db() as db:
            row = db.execute(
                "SELECT ref, apk, sha, md5, status FROM scans WHERE ref=? AND engagement=? "
                "AND run=? AND apk=? AND expires>?",
                (scan_ref, engagement_id, run_id, apk_ref, time.time()),
            ).fetchone()
        if row is None:
            raise MobSFError("mobsf_scope_denied")
        return row

    def _status(self, scan_ref: str, status: str) -> None:
        with self._db() as db:
            db.execute("UPDATE scans SET status=? WHERE ref=?", (status, scan_ref))

    def _output(self, row: tuple, **kwargs) -> MobSFOutput:
        return MobSFOutput(
            apk_ref=row[1],
            scan_ref=row[0],
            sha256=row[2],
            status=row[4],
            image=self.policy.image,
            **kwargs,
        )

    async def submit(self, apk_ref: str, expected_sha256: str, engagement_id: str, run_id: str):
        self._context(engagement_id, run_id, apk_ref)
        data = self._apk(apk_ref, expected_sha256)
        async with self.lock:
            with self._db() as db:
                count = db.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            if count >= self.policy.max_scans:
                raise MobSFError("mobsf_scan_limit")
            _, upload = await self._request(
                "upload",
                files={"file": ("approved.apk", data, "application/vnd.android.package-archive")},
            )
            md5 = upload.get("hash")
            if (
                not isinstance(md5, str)
                or not _MD5.fullmatch(md5)
                or md5 != hashlib.md5(data, usedforsecurity=False).hexdigest()
                or upload.get("scan_type") != "apk"
            ):
                raise MobSFError("mobsf_upload_schema_drift")
            ref = "scan_" + secrets.token_hex(16)
            with self._db() as db:
                db.execute(
                    "INSERT INTO scans VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ref,
                        engagement_id,
                        run_id,
                        apk_ref,
                        expected_sha256,
                        md5,
                        "uploaded",
                        time.time() + self.policy.retention_seconds,
                    ),
                )
            return self._output((ref, apk_ref, expected_sha256, md5, "uploaded"))

    async def start(self, apk_ref: str, scan_ref: str, engagement_id: str, run_id: str):
        async with self.lock:
            row = self._record(apk_ref, scan_ref, engagement_id, run_id)
            if row[4] != "uploaded":
                raise MobSFError("mobsf_scan_already_started")
            self._status(scan_ref, "running")
            try:
                # Re-scan disabled: immutable APK/hash is shared by MobSF's content store.
                await self._request("scan", data={"hash": row[3], "re_scan": "0"})
                for _ in range(self.policy.max_polls):
                    code, report = await self._request("report_json", data={"hash": row[3]})
                    if code == 200:
                        if report.get("md5", report.get("hash")) != row[3]:
                            raise MobSFError("mobsf_report_hash_mismatch")
                        self._status(scan_ref, "completed")
                        return self._output(self._record(apk_ref, scan_ref, engagement_id, run_id))
                    await asyncio.sleep(self.policy.poll_interval_seconds)
                raise MobSFError("mobsf_poll_limit")
            except asyncio.CancelledError:
                self._status(scan_ref, "cancelled")
                raise
            except MobSFError as exc:
                self._status(
                    scan_ref,
                    "timeout"
                    if str(exc)
                    in {
                        "mobsf_timeout",
                        "mobsf_poll_limit",
                    }
                    else "failed",
                )
                raise

    async def status(self, apk_ref: str, scan_ref: str, engagement_id: str, run_id: str):
        row = self._record(apk_ref, scan_ref, engagement_id, run_id)
        if row[4] == "running":
            # No upstream task enumeration: only this run's owned content hash.
            code, report = await self._request("report_json", data={"hash": row[3]})
            if code == 200 and report.get("md5", report.get("hash")) == row[3]:
                self._status(scan_ref, "completed")
        return self._output(self._record(apk_ref, scan_ref, engagement_id, run_id))

    async def summary(
        self,
        apk_ref: str,
        scan_ref: str,
        sections: list[str],
        engagement_id: str,
        run_id: str,
    ):
        row = self._record(apk_ref, scan_ref, engagement_id, run_id)
        if not sections or len(sections) != len(set(sections)) or set(sections) - set(SECTIONS):
            raise MobSFError("mobsf_section_denied")
        if row[4] != "completed":
            raise MobSFError("mobsf_report_not_ready")
        _, report = await self._request("report_json", data={"hash": row[3]})
        if report.get("md5", report.get("hash")) != row[3]:
            raise MobSFError("mobsf_report_hash_mismatch")
        normalized, missing, truncated = normalize_report(report, sections)
        output = self._output(
            row, sections=normalized, missing_sections=missing, truncated=truncated
        )
        if len(output.model_dump_json().encode()) > self.policy.max_output_bytes:
            raise MobSFError("mobsf_output_too_large")
        # Defense against credentials accidentally appearing even in structural fields.
        key = self._key()
        return MobSFOutput.model_validate_json(output.model_dump_json().replace(key, "[REDACTED]"))


def _token(value) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def normalize_report(report: dict, sections: list[str]) -> tuple[dict, list[str], bool]:
    """No raw source, secret values, HTML descriptions, URLs or log bodies."""
    result, missing, truncated = {}, [], False
    for section in sections:
        source = {
            "manifest": "manifest_analysis",
            "permission": "permissions",
            "secret": "secrets",
            "network": "network_security",
        }[section]
        raw = report.get(source)
        if raw is None:
            missing.append(section)
            continue
        if section == "permission":
            if not isinstance(raw, dict):
                raise MobSFError("mobsf_schema_drift")
            names = [name for name in raw if _token(name)]
            result[section] = {"names": names[:100], "count": len(raw), "source": source}
            truncated |= len(raw) > 100
        elif section == "secret":
            if not isinstance(raw, list):
                raise MobSFError("mobsf_schema_drift")
            result[section] = {
                "count": len(raw),
                "candidates": [
                    {"value": "[REDACTED]", "source": f"{source}/{i}"}
                    for i in range(min(100, len(raw)))
                ],
            }
            truncated |= len(raw) > 100
        else:
            if isinstance(raw, dict):
                raw = raw.get("manifest_findings" if section == "manifest" else "network_findings")
            if not isinstance(raw, list):
                raise MobSFError("mobsf_schema_drift")
            findings = []
            for i, item in enumerate(raw[:100]):
                if not isinstance(item, dict):
                    raise MobSFError("mobsf_schema_drift")
                findings.append(
                    {
                        "rule": _token(item.get("rule")),
                        "severity": item.get("severity")
                        if item.get("severity")
                        in {
                            "high",
                            "warning",
                            "info",
                            "secure",
                            "good",
                        }
                        else None,
                        "source": f"{source}/{i}",
                    }
                )
            result[section] = {"findings": findings, "count": len(raw)}
            truncated |= len(raw) > 100
    return result, missing, truncated


def create_server(policy: MobSFPolicy) -> MCPServer:
    client = MobSFClient(policy)
    server = MCPServer("mobsf-static-analysis", log_level="WARNING")

    @server.tool()
    async def mobsf_submit_apk(
        apk_ref: str,
        expected_sha256: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> MobSFOutput:
        """Upload a registered, approved APK with its pinned SHA-256; approval required."""
        return await client.submit(apk_ref, expected_sha256, engagement_id, run_id)

    @server.tool()
    async def mobsf_start_scan(
        apk_ref: str,
        scan_ref: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> MobSFOutput:
        """Start bounded static analysis of this run's owned APK; approval required."""
        return await client.start(apk_ref, scan_ref, engagement_id, run_id)

    @server.tool()
    async def mobsf_scan_status(
        apk_ref: str,
        scan_ref: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> MobSFOutput:
        """Read only this run's scan status; no scan enumeration."""
        return await client.status(apk_ref, scan_ref, engagement_id, run_id)

    @server.tool()
    async def mobsf_report_summary(
        apk_ref: str,
        scan_ref: str,
        sections: list[str],
        engagement_id: str = "",
        run_id: str = "",
    ) -> MobSFOutput:
        """Read selected static report sections; secret values are never returned."""
        return await client.summary(apk_ref, scan_ref, sections, engagement_id, run_id)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    create_server(MobSFPolicy.load(args.policy, args.sha256)).run(transport="stdio")


if __name__ == "__main__":
    main()
