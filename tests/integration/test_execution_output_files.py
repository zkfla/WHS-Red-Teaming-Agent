from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from redteam.capability.servers.execution import (
    DockerExecutionEngine,
    ExecutionError,
    ExecutionPolicy,
)
from redteam.capability.servers.workspace import WorkspacePolicy, create_server


class CompletedProcess:
    returncode: int | None = 0

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


def policy(workspace: Path, **updates: Any) -> ExecutionPolicy:
    base = ExecutionPolicy(
        asset_ref="workspace-test",
        engagement_id="eng-test",
        run_id="run-test",
        workspace_root=str(workspace.resolve()),
        image="example/text-tools@sha256:" + "1" * 64,
        allowed_executables={"head": "/usr/bin/head"},
        max_output_bytes=1024,
        max_output_file_bytes=2048,
    )
    return base.model_copy(update=updates)


async def execute(
    engine: DockerExecutionEngine,
    *,
    output_id: str,
    asset_ref: str = "workspace-test",
    engagement_id: str = "eng-test",
    run_id: str = "run-test",
):
    return await engine.run_command(
        asset_ref,
        "head",
        ["input.txt"],
        5,
        engagement_id,
        run_id,
        output_id=output_id,
    )


async def test_large_output_is_saved_with_an_independent_file_limit(
    tmp_path: Path,
) -> None:
    payload = b"x" * 5000

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        return CompletedProcess(payload)

    result = await execute(
        DockerExecutionEngine(policy(tmp_path), process_factory=spawn),
        output_id="large-output",
    )
    stored = tmp_path / result.output_path

    assert result.truncated is True
    assert len(result.stdout.encode()) == 1024
    assert result.output_path == ".command_outputs/large-output.txt"
    assert result.output_file_truncated is True
    assert stored.stat().st_size == 2048
    assert stored.read_text(encoding="utf-8").startswith("[stdout]\n")


async def test_output_identity_cannot_escape_or_overwrite(tmp_path: Path) -> None:
    starts = 0

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess(b"ok\n")

    engine = DockerExecutionEngine(policy(tmp_path), process_factory=spawn)
    first = await execute(engine, output_id="fixed-output")
    with pytest.raises(ExecutionError, match="execution_output_exists"):
        await execute(engine, output_id="fixed-output")
    with pytest.raises(ExecutionError, match="execution_invalid_output_identity"):
        await execute(engine, output_id="../outside")

    assert (tmp_path / first.output_path).read_text(encoding="utf-8") == "[stdout]\nok\n"
    assert starts == 1
    assert not (tmp_path.parent / "outside.txt").exists()


async def test_context_mismatch_cannot_create_an_output_file(tmp_path: Path) -> None:
    starts = 0

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess(b"nope")

    engine = DockerExecutionEngine(policy(tmp_path), process_factory=spawn)

    with pytest.raises(ExecutionError, match="execution_context_mismatch"):
        await execute(engine, output_id="wrong-run", run_id="run-other")
    with pytest.raises(ExecutionError, match="execution_context_mismatch"):
        await execute(engine, output_id="wrong-asset", asset_ref="workspace-other")

    assert starts == 0
    assert not (tmp_path / ".command_outputs").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor safety and mode checks")
async def test_output_directory_symlink_is_rejected_before_container_start(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".command_outputs").symlink_to(outside, target_is_directory=True)
    starts = 0

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        nonlocal starts
        starts += 1
        return CompletedProcess(b"nope")

    with pytest.raises(ExecutionError, match="execution_output_path_unsafe"):
        await execute(
            DockerExecutionEngine(policy(tmp_path), process_factory=spawn),
            output_id="symlink-blocked",
        )

    assert starts == 0
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="Workspace MCP requires POSIX safe IO")
async def test_workspace_read_file_reads_private_execution_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    async def spawn(*_: Any, **__: Any) -> CompletedProcess:
        return CompletedProcess(b"alpha\nbeta\n")

    result = await execute(
        DockerExecutionEngine(policy(workspace), process_factory=spawn),
        output_id="read-back",
    )
    output_directory = workspace / ".command_outputs"
    output_file = workspace / result.output_path
    assert stat.S_IMODE(output_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(output_file.stat().st_mode) == 0o600

    workspace_server = create_server(
        WorkspacePolicy(
            asset_ref="workspace-test",
            engagement_id="eng-test",
            run_id="run-test",
            root=str(workspace),
        ),
        mode="read",
    )
    async with Client(workspace_server) as client:
        reread = await client.call_tool(
            "read_file",
            {
                "asset_ref": "workspace-test",
                "path": result.output_path,
                "engagement_id": "eng-test",
                "run_id": "run-test",
            },
        )

    assert reread.is_error is False
    assert reread.structured_content["content"] == "[stdout]\nalpha\nbeta\n"


@pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)
async def test_real_docker_large_output_is_saved_and_read_via_workspace_mcp(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_bytes(b"output-line\n" * 10_000)
    configured = policy(workspace).model_copy(
        update={
            "image": os.environ["REDTEAM_TEST_EXECUTION_IMAGE"],
            "container_user": f"{os.getuid()}:{os.getgid()}",
            "max_output_file_bytes": 8192,
        }
    )
    result = await DockerExecutionEngine(configured).run_command(
        "workspace-test",
        "head",
        ["-c", "65536", "input.txt"],
        10,
        "eng-test",
        "run-test",
        output_id="docker-large-output",
    )

    assert result.exit_code == 0
    assert result.truncated is True
    assert result.output_file_truncated is True
    assert (workspace / result.output_path).stat().st_size == 8192
    workspace_server = create_server(
        WorkspacePolicy(
            asset_ref="workspace-test",
            engagement_id="eng-test",
            run_id="run-test",
            root=str(workspace),
        ),
        mode="read",
    )
    async with Client(workspace_server) as client:
        reread = await client.call_tool(
            "read_file",
            {
                "asset_ref": "workspace-test",
                "path": result.output_path,
                "engagement_id": "eng-test",
                "run_id": "run-test",
                "max_chars": 16_000,
            },
        )
    assert reread.is_error is False
    assert reread.structured_content["content"].startswith("[stdout]\noutput-line\n")
