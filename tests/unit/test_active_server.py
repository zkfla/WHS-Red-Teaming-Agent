"""Active scan concurrency and scope gates, using fake resolution and subprocess calls."""

from __future__ import annotations

import asyncio

from mcp import Client

from redteam.capability.servers import active
from redteam.capability.servers.config import ServerPolicy, TargetProfile
from redteam.capability.servers.network import ResolvedTarget, ToolInputError


def _policy(*, active_enabled: bool = True) -> ServerPolicy:
    return ServerPolicy(
        targets={
            "lab-a": TargetProfile(origin="http://a.lab", allowed_paths=["/"]),
            "lab-b": TargetProfile(origin="http://b.lab", allowed_paths=["/"]),
        },
        allowed_networks=["203.0.113.0/24"],
        active_enabled=active_enabled,
        active_ports=[80, 443],
        max_concurrency=2,
    )


async def test_independent_approved_targets_scan_concurrently(monkeypatch) -> None:
    policy = _policy()
    addresses = {"lab-a": "203.0.113.10", "lab-b": "203.0.113.20"}

    async def fake_resolve(policy: ServerPolicy, target_ref: str) -> ResolvedTarget:
        return ResolvedTarget(
            policy.targets[target_ref], target_ref, 80, (addresses[target_ref],)
        )

    first_two_started = asyncio.Event()
    release = asyncio.Event()
    active_count = 0
    maximum_active = 0

    async def fake_run_nmap(address: str, ports: list[int], timeout: float) -> bytes:
        nonlocal active_count, maximum_active
        active_count += 1
        maximum_active = max(maximum_active, active_count)
        if active_count == 2:
            first_two_started.set()
        try:
            await release.wait()
            return (
                b'<nmaprun><host><ports><port protocol="tcp" portid="80">'
                b'<state state="open"/></port></ports></host>'
                b'<runstats><finished exit="success"/></runstats></nmaprun>'
            )
        finally:
            active_count -= 1

    monkeypatch.setattr(active, "resolve_target", fake_resolve)
    monkeypatch.setattr(active, "run_nmap", fake_run_nmap)
    async with Client(active.create_server(policy)) as client:
        calls = [
            asyncio.create_task(client.call_tool("port_scan", {"target_ref": target_ref}))
            for target_ref in ("lab-a", "lab-b")
        ]
        try:
            await asyncio.wait_for(first_two_started.wait(), 1)
        finally:
            release.set()
        results = await asyncio.gather(*calls)

    assert maximum_active == 2
    assert all(result.is_error is False for result in results)


async def test_scope_rejection_prevents_nmap_launch(monkeypatch) -> None:
    policy = _policy()
    launches: list[str] = []

    async def reject_target(policy: ServerPolicy, target_ref: str) -> ResolvedTarget:
        raise ToolInputError("target address outside configured networks")

    async def fake_run_nmap(address: str, ports: list[int], timeout: float) -> bytes:
        launches.append(address)
        return b""

    monkeypatch.setattr(active, "resolve_target", reject_target)
    monkeypatch.setattr(active, "run_nmap", fake_run_nmap)
    async with Client(active.create_server(policy)) as client:
        result = await client.call_tool("port_scan", {"target_ref": "lab-a"})

    assert result.is_error is True
    assert launches == []


async def test_operator_active_enablement_still_gates_scope_and_execution(monkeypatch) -> None:
    policy = _policy(active_enabled=False)
    calls: list[str] = []

    async def resolve(policy: ServerPolicy, target_ref: str) -> ResolvedTarget:
        calls.append("resolve")
        raise AssertionError("disabled active scanning must stop before scope resolution")

    async def fake_run_nmap(address: str, ports: list[int], timeout: float) -> bytes:
        calls.append("run")
        return b""

    monkeypatch.setattr(active, "resolve_target", resolve)
    monkeypatch.setattr(active, "run_nmap", fake_run_nmap)
    async with Client(active.create_server(policy)) as client:
        result = await client.call_tool("port_scan", {"target_ref": "lab-a"})

    assert result.is_error is True
    assert calls == []
