"""Local fallback MCP server for the ``mobile`` capability.

External MCP servers can provide app launch, device logs or crash retrieval
through their own adapter binding
(``REDTEAM_MCP_<SERVER_ID>_TOOL_OPERATIONS_JSON``, see
``deploy/mcp/README.md``). This module implements only the ``device_shell`` operation
(docs/contracts/actions.md) as a narrow, allowlisted ADB diagnostic tool —
following the same policy-scoped, real-but-bounded pattern as
``servers/recon.py``/``servers/web_http.py``.

Runtime instrumentation, UI automation, installation and arbitrary/generated
code execution are not provided by this server. Other providers need explicit
tool registration, target binding and operator policy, not a general shell
fallback. Discovery does not verify a connected device or installed app.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.core.contracts.tool import (
    PROVIDER_METADATA_ARGUMENT_BINDINGS,
    PROVIDER_METADATA_COMMAND_FIELDS,
    PROVIDER_METADATA_TARGET_TYPES,
)

# Read-only/diagnostic ADB shell subcommands only. Values are the exact argv
# appended after ``adb -s <serial> shell`` — no operator-supplied argv, no
# shell string interpolation.
_ALLOWED_ADB_COMMANDS: dict[str, tuple[str, ...]] = {
    "getprop": ("getprop",),
    "pm_list_packages": ("pm", "list", "packages"),
    "dumpsys_battery": ("dumpsys", "battery"),
    "logcat_recent": ("logcat", "-d", "-t", "200"),
}

_MAX_OUTPUT_BYTES = 8192


class ToolInputError(ValueError):
    """Messages contain only fixed explanations, never raw device output."""


class DeviceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    serial: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")


class MobileDiagnosticsPolicy(BaseModel):
    """Operator-owned device allowlist; never populated by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    devices: dict[str, DeviceProfile] = Field(min_length=1)
    allowed_commands: list[str] = Field(default_factory=lambda: sorted(_ALLOWED_ADB_COMMANDS))
    adb_path: str = "adb"
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    max_output_bytes: int = Field(default=_MAX_OUTPUT_BYTES, ge=1024, le=65536)

    @field_validator("allowed_commands")
    @classmethod
    def known_commands_only(cls, values: list[str]) -> list[str]:
        if not values or len(values) != len(set(values)):
            raise ValueError("allowed_commands must be non-empty and unique")
        unknown = sorted(set(values) - set(_ALLOWED_ADB_COMMANDS))
        if unknown:
            raise ValueError(f"unknown diagnostic commands: {', '.join(unknown)}")
        return values

    @field_validator("adb_path")
    @classmethod
    def single_executable(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "\x00\n\r"):
            raise ValueError("adb_path must be a single executable name or path")
        return value

    @classmethod
    def load(cls, path: str, *, expected_sha256: str | None = None) -> MobileDiagnosticsPolicy:
        with Path(path).open("rb") as source:
            content = source.read(65537)
        if len(content) > 65536:
            raise ToolInputError("mobile policy is oversized")
        if expected_sha256 is not None and (
            re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise ToolInputError("mobile policy digest mismatch")
        return cls.model_validate(json.loads(content))


class DeviceShellOutput(BaseModel):
    """Fields returned by the bounded ADB diagnostic MCP tool."""

    device_ref: str
    command: str
    output: str
    truncated: bool


async def _run_adb(
    argv: list[str], timeout: float, max_output_bytes: int = _MAX_OUTPUT_BYTES,
) -> str:
    """Spawn one bounded, non-shell ADB command and return decoded stdout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_MAX_OUTPUT_BYTES,
        )
    except OSError as exc:
        raise ToolInputError("adb executable is unavailable") from exc
    async def capture() -> bytes:
        # Drain stdout to avoid blocking the child, but retain only a bounded
        # prefix plus one overflow byte. The time allowance includes draining
        # and process exit; no unbounded communicate() buffer is allocated.
        assert process.stdout is not None
        retained = bytearray()
        while chunk := await process.stdout.read(_MAX_OUTPUT_BYTES):
            remaining = max_output_bytes + 1 - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
        await process.wait()
        return bytes(retained)

    try:
        output = await asyncio.wait_for(capture(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ToolInputError("adb command timed out") from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode != 0:
        raise ToolInputError("adb command exited with a non-zero status")
    return output.decode("utf-8", errors="replace")


def create_server(policy: MobileDiagnosticsPolicy) -> MCPServer:
    server = MCPServer("mobile-diagnostics", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool(meta={
        PROVIDER_METADATA_TARGET_TYPES: ["device"],
        PROVIDER_METADATA_ARGUMENT_BINDINGS: {"device_ref": "device_ref"},
        PROVIDER_METADATA_COMMAND_FIELDS: ["command"],
    })
    async def device_shell(device_ref: str, command: str) -> DeviceShellOutput:
        """Run one allowlisted, read-only ADB diagnostic against a configured device.

        Not a general remote-shell primitive: ``command`` selects a fixed,
        operator-approved diagnostic (see ``allowed_commands``); arbitrary
        shell strings are rejected outright.
        """
        device = policy.devices.get(device_ref)
        if device is None:
            raise ToolInputError("unknown device_ref")
        if command not in policy.allowed_commands:
            allowed = ", ".join(sorted(policy.allowed_commands))
            raise ToolInputError(f"command must be one of: {allowed}")
        argv = [
            policy.adb_path,
            "-s",
            device.serial,
            "shell",
            *_ALLOWED_ADB_COMMANDS[command],
        ]
        async with execution_lock:
            output = await _run_adb(argv, policy.timeout_seconds, policy.max_output_bytes)
        encoded = output.encode("utf-8")
        return DeviceShellOutput(
            device_ref=device_ref,
            command=command,
            output=encoded[:policy.max_output_bytes].decode("utf-8", errors="ignore"),
            truncated=len(encoded) > policy.max_output_bytes,
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--policy-sha256")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args()
    server = create_server(MobileDiagnosticsPolicy.load(
        args.policy, expected_sha256=args.policy_sha256,
    ))
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
