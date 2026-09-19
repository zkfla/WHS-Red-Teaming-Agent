"""Narrow Nmap wrapper: one validated address, a fixed short port list, no scripts."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server import MCPServer

from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import ToolInputError, resolve_target


async def run_nmap(address: str, ports: list[int], timeout: float) -> bytes:
    args = [
        "nmap",
        "-n",
        "-Pn",
        "-sT",
        "--max-retries",
        "0",
        "--max-rate",
        "5",
        "--host-timeout",
        f"{int(timeout)}s",
        "-p",
        ",".join(map(str, ports)),
        "-oX",
        "-",
    ]
    if ":" in address:
        args.append("-6")
    args.append(address)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolInputError("Nmap executable is unavailable") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout + 2)
        if process.returncode != 0 or len(output) > 262144:
            raise ToolInputError("Nmap failed or exceeded output limit")
        return output
    except asyncio.TimeoutError as exc:
        raise ToolInputError("Nmap execution timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def create_server(policy: ServerPolicy) -> MCPServer:
    server = MCPServer("recon-active", log_level="WARNING")
    execution_lock = asyncio.Lock()

    @server.tool()
    def active_status() -> dict[str, Any]:
        """Report operator enablement; this is not a Control Plane approval token."""
        return {"enabled": policy.active_enabled, "allowed_ports": policy.active_ports}

    @server.tool()
    async def port_scan(target_ref: str) -> dict[str, Any]:
        """Bounded TCP connect scan for a configured lab target. Disabled by default.

        Requires Control Plane approval AND operator active_enabled. No custom flags or NSE.
        """
        if not policy.active_enabled:
            raise ToolInputError("active scanning is disabled by operator configuration")
        async with execution_lock:
            target = await resolve_target(policy, target_ref)
            output = await run_nmap(
                target.addresses[0], policy.active_ports, policy.timeout_seconds
            )
        try:
            root = ET.fromstring(output)
        except ET.ParseError as exc:
            raise ToolInputError("invalid Nmap XML result") from exc
        ports = []
        for item in root.findall("./host/ports/port"):
            state = item.find("state")
            ports.append(
                {
                    "port": int(item.attrib["portid"]),
                    "protocol": item.attrib["protocol"],
                    "state": state.attrib.get("state", "unknown")
                    if state is not None
                    else "unknown",
                }
            )
        finished = root.find("./runstats/finished")
        complete = finished is not None and finished.attrib.get("exit") == "success"
        return {
            "target_ref": target_ref,
            "ports": ports,
            "engine_exit_success": complete,
            "verdict": "observation_only",
        }

    return server
