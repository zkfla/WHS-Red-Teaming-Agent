"""Policy validation and allowlisted-argv behavior for the mobile fallback server."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import Client
from pydantic import ValidationError

from redteam.capability.servers import mobile


@pytest.fixture
def policy() -> mobile.MobileDiagnosticsPolicy:
    return mobile.MobileDiagnosticsPolicy.model_validate(
        {
            "devices": {"pixel": {"serial": "emulator-5554"}},
            "allowed_commands": ["getprop", "dumpsys_battery"],
            "adb_path": "adb",
        }
    )


def test_unknown_diagnostic_command_rejected_before_any_device_access() -> None:
    with pytest.raises(ValidationError, match="unknown diagnostic commands"):
        mobile.MobileDiagnosticsPolicy.model_validate(
            {
                "devices": {"pixel": {"serial": "emulator-5554"}},
                "allowed_commands": ["shell_rm_rf"],
            }
        )


def test_empty_allowlist_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        mobile.MobileDiagnosticsPolicy.model_validate(
            {"devices": {"pixel": {"serial": "emulator-5554"}}, "allowed_commands": []}
        )


def test_device_serial_must_match_safe_pattern() -> None:
    with pytest.raises(ValidationError):
        mobile.MobileDiagnosticsPolicy.model_validate(
            {"devices": {"pixel": {"serial": "emulator-5554; rm -rf /"}}}
        )


def test_concurrency_policy_is_bounded_and_omits_compatible_default() -> None:
    default = mobile.MobileDiagnosticsPolicy.model_validate(
        {"devices": {"pixel": {"serial": "emulator-5554"}}}
    )
    assert default.max_concurrency == 1
    assert "max_concurrency" not in default.model_dump(mode="json")

    configured = mobile.MobileDiagnosticsPolicy.model_validate(
        {"devices": {"pixel": {"serial": "emulator-5554"}}, "max_concurrency": 3}
    )
    assert configured.model_dump(mode="json")["max_concurrency"] == 3
    for value in (0, 33):
        with pytest.raises(ValidationError):
            mobile.MobileDiagnosticsPolicy.model_validate(
                {"devices": {"pixel": {"serial": "emulator-5554"}}, "max_concurrency": value}
            )


async def test_unknown_device_ref_rejected(policy: mobile.MobileDiagnosticsPolicy) -> None:
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "nope", "command": "getprop"}
        )
    assert result.is_error is True


async def test_command_outside_allowlist_rejected(policy: mobile.MobileDiagnosticsPolicy) -> None:
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "pixel", "command": "logcat_recent"}
        )
    assert result.is_error is True


async def test_allowed_command_runs_fixed_non_shell_argv(
    monkeypatch: pytest.MonkeyPatch, policy: mobile.MobileDiagnosticsPolicy
) -> None:
    calls: list[list[str]] = []

    async def fake_run_adb(argv: list[str], timeout: float) -> str:
        calls.append(argv)
        return "[ro.product]: [pixel]\n"

    monkeypatch.setattr(mobile, "_run_adb", fake_run_adb)
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "pixel", "command": "getprop"}
        )
    assert result.is_error is False
    assert result.structured_content == {
        "device_ref": "pixel",
        "command": "getprop",
        "output": "[ro.product]: [pixel]\n",
        "truncated": False,
    }
    assert calls == [["adb", "-s", "emulator-5554", "shell", "getprop"]]


async def test_output_is_truncated_at_fixed_byte_budget(
    monkeypatch: pytest.MonkeyPatch, policy: mobile.MobileDiagnosticsPolicy
) -> None:
    async def fake_run_adb(argv: list[str], timeout: float) -> str:
        return "x" * (mobile._MAX_OUTPUT_BYTES + 500)

    monkeypatch.setattr(mobile, "_run_adb", fake_run_adb)
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "pixel", "command": "dumpsys_battery"}
        )
    assert result.structured_content is not None
    assert result.structured_content["truncated"] is True
    assert len(result.structured_content["output"]) == mobile._MAX_OUTPUT_BYTES


async def test_non_zero_exit_surfaces_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - not reached on clean exit
            pass

        async def wait(self) -> int:  # pragma: no cover
            return 1

    async def create(*args: Any, **kwargs: Any) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(mobile.ToolInputError, match="non-zero"):
        await mobile._run_adb(["adb", "-s", "emulator-5554", "shell", "getprop"], 5)


async def test_hung_adb_process_is_killed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = None
        killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    process = Process()

    async def create(*args: Any, **kwargs: Any) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(mobile.ToolInputError, match="timed out"):
        await mobile._run_adb(["adb", "-s", "emulator-5554", "shell", "getprop"], 0.05)
    assert process.killed


async def test_independent_devices_overlap_while_same_serial_waiters_do_not_take_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = mobile.MobileDiagnosticsPolicy.model_validate(
        {
            "devices": {
                "phone": {"serial": "emulator-5554"},
                "phone_alias": {"serial": "emulator-5554"},
                "tablet": {"serial": "emulator-5556"},
            },
            "max_concurrency": 2,
        }
    )
    phone_entered = asyncio.Event()
    tablet_entered = asyncio.Event()
    release = asyncio.Event()
    active_by_serial: dict[str, int] = {}
    max_by_serial: dict[str, int] = {}
    overall_active = 0
    overall_max = 0

    async def fake_run_adb(argv: list[str], timeout: float) -> str:
        nonlocal overall_active, overall_max
        serial = argv[2]
        active_by_serial[serial] = active_by_serial.get(serial, 0) + 1
        max_by_serial[serial] = max(max_by_serial.get(serial, 0), active_by_serial[serial])
        overall_active += 1
        overall_max = max(overall_max, overall_active)
        if serial == "emulator-5554":
            phone_entered.set()
        else:
            tablet_entered.set()
        try:
            await release.wait()
            return "ok"
        finally:
            active_by_serial[serial] -= 1
            overall_active -= 1

    monkeypatch.setattr(mobile, "_run_adb", fake_run_adb)
    async with Client(mobile.create_server(policy)) as client:
        phone_first = asyncio.create_task(
            client.call_tool("device_shell", {"device_ref": "phone", "command": "getprop"})
        )
        await asyncio.wait_for(phone_entered.wait(), 1)
        phone_second = asyncio.create_task(
            client.call_tool(
                "device_shell", {"device_ref": "phone_alias", "command": "dumpsys_battery"}
            )
        )
        # Give the second MCP request time to reach the per-serial lock before
        # the independent device competes for the remaining global slot.
        await asyncio.sleep(0.05)
        tablet = asyncio.create_task(
            client.call_tool("device_shell", {"device_ref": "tablet", "command": "getprop"})
        )
        try:
            await asyncio.wait_for(tablet_entered.wait(), 1)
        finally:
            release.set()
        results = await asyncio.gather(phone_first, phone_second, tablet)

    assert all(result.is_error is False for result in results)
    assert max_by_serial["emulator-5554"] == 1
    assert overall_max == 2
