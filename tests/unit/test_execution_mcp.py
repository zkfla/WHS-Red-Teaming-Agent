from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from redteam.capability.servers.execution import (
    DockerExecutionEngine,
    ExecutionError,
    ExecutionPolicy,
    ensure_runtime_available,
)


class CompletedProcess:
    def __init__(
        self,
        stdout: bytes = b"ok\n",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode: int | None = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


class HangingProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._finished = asyncio.Event()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._finished.set()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def policy(tmp_path: Path) -> ExecutionPolicy:
    return ExecutionPolicy(
        asset_ref="workspace-test",
        engagement_id="eng-test",
        run_id="run-test",
        workspace_root=str(tmp_path.resolve()),
        image="example/text-tools@sha256:" + "1" * 64,
        allowed_executables={
            "grep": "/usr/bin/grep",
            "sort": "/usr/bin/sort",
            "wc": "/usr/bin/wc",
        },
    )


async def test_run_command_builds_locked_down_docker_argv_and_minimal_environment(
    policy: ExecutionPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    process = CompletedProcess()

    async def spawn(*argv: str, **kwargs: Any) -> CompletedProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setenv("TOP_SECRET", "must-not-leak")
    engine = DockerExecutionEngine(policy, process_factory=spawn)
    result = await engine.run_command(
        "workspace-test",
        "grep",
        ["needle", "input.txt"],
        3,
        "eng-test",
        "run-test",
    )

    argv = captured["argv"]
    assert argv[0] == "docker"
    assert ("--network", "none") == argv[argv.index("--network") : argv.index("--network") + 2]
    assert "--read-only" in argv
    assert ("--cap-drop", "ALL") == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "no-new-privileges" in argv
    assert "--pids-limit" in argv and "--memory" in argv and "--cpus" in argv
    mount = argv[argv.index("--mount") + 1]
    assert mount == f"type=bind,src={policy.workspace_root},dst=/workspace,readonly"
    assert argv[-4:] == (policy.image, "/usr/bin/grep", "needle", "input.txt")
    assert captured["kwargs"]["env"] == {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    assert "TOP_SECRET" not in captured["kwargs"]["env"]
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.timed_out is False
    assert result.truncated is False


@pytest.mark.parametrize(
    ("executable", "args", "message"),
    [
        ("python", ["-c", "print(1)"], "execution_executable_not_allowed"),
        ("grep", ["needle;cat", "input.txt"], "execution_shell_syntax_forbidden"),
        ("grep", ["needle", "../secret"], "execution_invalid_path"),
        ("grep", ["needle", "/etc/passwd"], "execution_invalid_path"),
        ("grep", ["needle", r"C:\\secret"], "execution_invalid_path"),
    ],
)
async def test_command_policy_rejects_unsafe_inputs_before_spawn(
    policy: ExecutionPolicy,
    executable: str,
    args: list[str],
    message: str,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> CompletedProcess:
        pytest.fail("invalid command must not start Docker")

    engine = DockerExecutionEngine(policy, process_factory=fail)
    with pytest.raises(ExecutionError, match=message):
        await engine.run_command(
            "workspace-test", executable, args, 3, "eng-test", "run-test"
        )


async def test_context_and_timeout_are_checked_before_spawn(policy: ExecutionPolicy) -> None:
    async def fail(*args: Any, **kwargs: Any) -> CompletedProcess:
        pytest.fail("invalid context must not start Docker")

    engine = DockerExecutionEngine(policy, process_factory=fail)
    with pytest.raises(ExecutionError, match="execution_context_mismatch"):
        await engine.run_command("workspace-other", "wc", ["input.txt"], 1, "eng-test", "run-test")
    with pytest.raises(ExecutionError, match="execution_invalid_timeout"):
        await engine.run_command(
            "workspace-test", "wc", ["input.txt"], 31, "eng-test", "run-test"
        )


def test_policy_rejects_unpinned_image_shells_and_broad_workspace(tmp_path: Path) -> None:
    values = {
        "asset_ref": "workspace-test",
        "engagement_id": "eng-test",
        "run_id": "run-test",
        "workspace_root": str(tmp_path),
        "image": "example/text-tools:latest",
        "allowed_executables": {"grep": "/usr/bin/grep"},
    }
    with pytest.raises(ValueError, match="pinned by sha256"):
        ExecutionPolicy(**values)
    values["image"] = "example/text-tools@sha256:" + "1" * 64
    values["allowed_executables"] = {"sh": "/usr/bin/sh"}
    with pytest.raises(ValueError, match="unsafe executable"):
        ExecutionPolicy(**values)
    values["allowed_executables"] = {"grep": "/usr/bin/grep"}
    values["workspace_root"] = str(Path(Path.cwd().anchor))
    with pytest.raises(ValueError, match="narrow absolute path"):
        ExecutionPolicy(**values)


def test_server_start_refuses_windows_or_missing_docker(
    policy: ExecutionPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        with pytest.raises(ExecutionError, match="execution_platform_unsupported"):
            ensure_runtime_available(policy)
        return

    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ExecutionError, match="execution_docker_unavailable"):
        ensure_runtime_available(policy)


async def test_nonzero_exit_is_distinct_from_transport_failure(
    policy: ExecutionPolicy,
) -> None:
    async def spawn(*args: Any, **kwargs: Any) -> CompletedProcess:
        return CompletedProcess(stderr=b"no match\n", returncode=2)

    result = await DockerExecutionEngine(policy, process_factory=spawn).run_command(
        "workspace-test", "grep", ["missing", "input.txt"], 3, "eng-test", "run-test"
    )
    assert result.exit_code == 2
    assert result.stderr == "no match\n"
    assert result.timed_out is False


async def test_output_is_bounded_and_reports_truncation(policy: ExecutionPolicy) -> None:
    process = CompletedProcess(
        stdout=b"a" * policy.max_output_bytes,
        stderr=b"secret-tail" * 100,
    )

    async def spawn(*args: Any, **kwargs: Any) -> CompletedProcess:
        return process

    result = await DockerExecutionEngine(policy, process_factory=spawn).run_command(
        "workspace-test", "sort", ["input.txt"], 3, "eng-test", "run-test"
    )
    assert len(result.stdout.encode()) == policy.max_output_bytes
    assert result.stderr == ""
    assert result.truncated is True


async def test_timeout_forces_container_and_process_cleanup(policy: ExecutionPolicy) -> None:
    running = HangingProcess()
    cleanup_calls: list[tuple[str, ...]] = []

    async def spawn(*args: Any, **kwargs: Any) -> HangingProcess:
        return running

    async def cleanup(*args: str, **kwargs: Any) -> CompletedProcess:
        cleanup_calls.append(args)
        return CompletedProcess()

    result = await DockerExecutionEngine(
        policy,
        process_factory=spawn,
        cleanup_process_factory=cleanup,
    ).run_command(
        "workspace-test", "wc", ["input.txt"], 0.01, "eng-test", "run-test"
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert running.killed is True
    assert running.returncode == -9
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][:3] == ("docker", "rm", "--force")


async def test_cancellation_forces_container_and_child_cleanup(
    policy: ExecutionPolicy,
) -> None:
    running = HangingProcess()
    active_containers: set[str] = set()

    async def spawn(*args: Any, **kwargs: Any) -> HangingProcess:
        active_containers.add(args[args.index("--name") + 1])
        return running

    async def cleanup(*args: str, **kwargs: Any) -> CompletedProcess:
        active_containers.discard(args[-1])
        return CompletedProcess()

    engine = DockerExecutionEngine(
        policy,
        process_factory=spawn,
        cleanup_process_factory=cleanup,
    )
    task = asyncio.create_task(
        engine.run_command(
            "workspace-test", "wc", ["input.txt"], 10, "eng-test", "run-test"
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert running.killed is True
    assert active_containers == set()
