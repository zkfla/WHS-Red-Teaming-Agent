"""Policy validation and allowlisted-argv behavior for the mobile fallback server."""

from __future__ import annotations

import asyncio
import sys
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

    async def fake_run_adb(argv: list[str], timeout: float, max_output_bytes: int) -> str:
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
    async def fake_run_adb(argv: list[str], timeout: float, max_output_bytes: int) -> str:
        return "x" * (mobile._MAX_OUTPUT_BYTES + 500)

    monkeypatch.setattr(mobile, "_run_adb", fake_run_adb)
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "pixel", "command": "dumpsys_battery"}
        )
    assert result.structured_content is not None
    assert result.structured_content["truncated"] is True
    assert len(result.structured_content["output"]) == mobile._MAX_OUTPUT_BYTES


async def test_multibyte_output_respects_utf8_byte_budget(monkeypatch, policy) -> None:
    async def fake_run_adb(argv, timeout, max_output_bytes=8192):
        return "가" * 4000

    monkeypatch.setattr(mobile, "_run_adb", fake_run_adb)
    async with Client(mobile.create_server(policy)) as client:
        result = await client.call_tool(
            "device_shell", {"device_ref": "pixel", "command": "getprop"}
        )
    assert result.structured_content["truncated"] is True
    assert len(result.structured_content["output"].encode("utf-8")) <= 8192


async def test_adb_output_is_streamed_with_bounded_reads(monkeypatch) -> None:
    class Output:
        remaining = 200_000
        reads: list[int] = []

        async def read(self, count):
            self.reads.append(count)
            chunk = b"x" * min(count, self.remaining)
            self.remaining -= len(chunk)
            return chunk

    class Process:
        returncode = None
        stdout = Output()

        async def communicate(self):
            raise AssertionError("unbounded communicate must not be used")

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    output = await mobile._run_adb(["adb", "-s", "test-serial", "shell", "getprop"], 1)
    assert len(output.encode("utf-8")) == mobile._MAX_OUTPUT_BYTES + 1
    assert process.stdout.remaining == 0
    assert max(process.stdout.reads) <= mobile._MAX_OUTPUT_BYTES


async def test_non_zero_exit_surfaces_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = 1

        class Output:
            async def read(self, count: int) -> bytes:
                return b""

        stdout = Output()

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

        class Output:
            async def read(self, count: int) -> bytes:
                await asyncio.sleep(60)
                return b""

        stdout = Output()

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


async def test_cancelled_capture_kills_only_owned_child(monkeypatch):
    entered = asyncio.Event()

    class Output:
        async def read(self, count):
            entered.set()
            await asyncio.Event().wait()

    class Process:
        returncode = None
        stdout = Output()
        killed = False
        waited = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9
            return -9

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(mobile._run_adb(["adb", "-s", "synthetic", "shell", "getprop"], 5))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited


async def test_real_local_subprocess_large_output_has_bounded_capture():
    # A synthetic child, NOT adb or a connected-device success test.
    output = await mobile._run_adb([
        sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 2000000)",
    ], 5)
    assert len(output.encode("utf-8")) == mobile._MAX_OUTPUT_BYTES + 1
