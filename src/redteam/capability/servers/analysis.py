"""Local fallback MCP server for the ``analysis`` capability.

The candidate external MCP server for this capability (apktool-mcp-server)
already covers ``static_analysis`` at the decompiled-code level (manifest,
smali, resources, in-project search) once bound via
``REDTEAM_MCP_<SERVER_ID>_TOOL_OPERATIONS_JSON`` — see ``deploy/mcp/README.md``.
Its write-capable tools (build/modify/clean) are deliberately excluded from
that binding.

This module is a complementary, dependency-free ``static_analysis``
implementation at the *APK container* level: it reads the zip structure of
an operator-registered ``.apk`` file directly (stdlib ``zipfile``), without
requiring apktool/Java to be installed. It does not decompile smali or parse
binary AndroidManifest.xml — that stays the job of an apktool-based server.
Useful standalone (or as a fallback) when no apktool MCP server is
configured yet, following the same policy-scoped, read-only pattern as
``servers/recon.py``/``servers/web_http.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolInputError(ValueError):
    """Messages contain only fixed explanations, never raw APK content."""


class ApkProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str

    @field_validator("path")
    @classmethod
    def existing_apk_file(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.suffix.lower() != ".apk" or not candidate.is_file():
            raise ValueError("apk path must point to an existing .apk file")
        return str(candidate)


class AnalysisServerPolicy(BaseModel):
    """Operator-owned APK registry; never populated by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    apks: dict[str, ApkProfile] = Field(min_length=1)
    max_listing_entries: int = Field(default=500, ge=1, le=5000)
    max_search_bytes: int = Field(default=1_048_576, ge=1024, le=8_388_608)

    @classmethod
    def load(cls, path: str) -> AnalysisServerPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class ApkEntry(BaseModel):
    name: str
    size: int
    compressed_size: int


class ApkFileListingOutput(BaseModel):
    apk_ref: str
    entries: list[ApkEntry]
    limit: int
    may_be_truncated: bool


class ApkEntryDigestOutput(BaseModel):
    apk_ref: str
    entry_name: str
    size: int
    sha256: str


class ApkStringSearchOutput(BaseModel):
    apk_ref: str
    entry_name: str
    pattern: str
    match_count: int
    match_offsets: list[int]
    may_be_truncated: bool


def _resolve(policy: AnalysisServerPolicy, apk_ref: str) -> Path:
    profile = policy.apks.get(apk_ref)
    if profile is None:
        raise ToolInputError("unknown apk_ref")
    return Path(profile.path)


def create_server(policy: AnalysisServerPolicy) -> MCPServer:
    server = MCPServer("analysis-apk-inventory", log_level="WARNING")

    @server.tool()
    def apk_file_listing(apk_ref: str) -> ApkFileListingOutput:
        """List zip entries inside a registered APK container; no decompilation."""
        path = _resolve(policy, apk_ref)
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()[: policy.max_listing_entries]
                entries = [
                    {
                        "name": info.filename[:512],
                        "size": info.file_size,
                        "compressed_size": info.compress_size,
                    }
                    for info in infos
                ]
        except zipfile.BadZipFile as exc:
            raise ToolInputError("registered file is not a valid zip/APK container") from exc
        return ApkFileListingOutput(
            apk_ref=apk_ref,
            entries=[ApkEntry.model_validate(entry) for entry in entries],
            limit=policy.max_listing_entries,
            may_be_truncated=len(entries) == policy.max_listing_entries,
        )

    @server.tool()
    def apk_entry_digest(apk_ref: str, entry_name: str) -> ApkEntryDigestOutput:
        """SHA-256 of one zip entry's decompressed bytes; content itself is never returned."""
        path = _resolve(policy, apk_ref)
        try:
            with zipfile.ZipFile(path) as archive:
                data = archive.read(entry_name)
        except zipfile.BadZipFile as exc:
            raise ToolInputError("registered file is not a valid zip/APK container") from exc
        except KeyError as exc:
            raise ToolInputError("unknown entry_name") from exc
        return ApkEntryDigestOutput(
            apk_ref=apk_ref,
            entry_name=entry_name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @server.tool()
    def apk_string_search(apk_ref: str, entry_name: str, pattern: str) -> ApkStringSearchOutput:
        """Fixed-string (non-regex) search within one bounded, text-decoded zip entry."""
        if not 2 <= len(pattern) <= 200:
            raise ToolInputError("pattern must be 2-200 characters")
        path = _resolve(policy, apk_ref)
        try:
            with zipfile.ZipFile(path) as archive:
                try:
                    info = archive.getinfo(entry_name)
                except KeyError as exc:
                    raise ToolInputError("unknown entry_name") from exc
                if info.file_size > policy.max_search_bytes:
                    raise ToolInputError("entry exceeds configured search size limit")
                data = archive.read(entry_name)
        except zipfile.BadZipFile as exc:
            raise ToolInputError("registered file is not a valid zip/APK container") from exc
        text = data.decode("utf-8", errors="ignore")
        offsets = [match.start() for match in re.finditer(re.escape(pattern), text)][:50]
        return ApkStringSearchOutput(
            apk_ref=apk_ref,
            entry_name=entry_name,
            pattern=pattern,
            match_count=len(offsets),
            match_offsets=offsets,
            may_be_truncated=len(offsets) == 50,
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8803)
    args = parser.parse_args()
    server = create_server(AnalysisServerPolicy.load(args.policy))
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
