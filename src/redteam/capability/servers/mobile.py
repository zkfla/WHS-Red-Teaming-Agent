"""Local fallback MCP server for the ``mobile`` capability.

Candidate external MCP servers for this capability (e.g. mobile-next/mobile-mcp)
cover ``app_launch`` and ``response_analysis``-shaped tooling (app launch,
device logs/crash retrieval) through their own adapter binding
(``REDTEAM_MCP_<SERVER_ID>_TOOL_OPERATIONS_JSON``, see
``deploy/mcp/README.md``). Neither currently exposes a shell-style primitive,
so this module implements only the ``device_shell`` canonical operation
(docs/contracts/actions.md) as a narrow, allowlisted ADB diagnostic tool --
following the same policy-scoped, real-but-bounded pattern as
``servers/recon.py``/``servers/web_http.py``.

This intentionally does not implement ``runtime_instrumentation``: hooking a
running process (e.g. via Frida) is a different class of tool than a bounded
diagnostic command and would mean building new attack tooling in-repo rather
than wrapping an existing one (see instruction.md section 21). That gap is
left for a dedicated MCP adapter/Issue once the team decides on one.

No UI-automation tools (tap/swipe/screenshot/install/uninstall) are exposed
here -- those are handled by the external mobile MCP candidate under its own
allowlist, not by this dependency-free fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Read-only/diagnostic ADB shell subcommands only. Values are the exact argv
# appended after ``adb -s <serial> shell`` -- no operator-supplied argv, no
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
    def load(cls, path: str) -> MobileDiagnosticsPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


async def _run_adb(argv: list[str], timeout: float) -> str:
    """Spawn one bounded, non-shell ADB command and return decoded stdout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolInputError("adb executable is unavailable") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ToolInputError("adb command timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise ToolInputError("adb command exited with a non-zero status")
    return output.decode("utf-8", errors="replace")


def create_server(policy: MobileDiagnosticsPolicy) -> MCPServer:
    server = MCPServer("mobile-diagnostics", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool()
    async def device_shell(device_ref: str, command: str) -> dict[str, Any]:
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
            output = await _run_adb(argv, policy.timeout_seconds)
        return {
            "device_ref": device_ref,
            "command": command,
            "output": output[:_MAX_OUTPUT_BYTES],
            "truncated": len(output) > _MAX_OUTPUT_BYTES,
        }

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args()
    server = create_server(MobileDiagnosticsPolicy.load(args.policy))
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
