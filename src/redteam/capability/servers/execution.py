"""Docker-only, run-scoped command execution MCP for approved workspaces.

The server never invokes a shell and never falls back to host execution.
Operator policy fixes one asset, engagement, run, workspace, image digest,
and exact executable allowlist before the MCP process starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROPOSED_OPERATION = "command_run"
REMOTE_TOOL_NAME = "run_command"
MODULE = "redteam.capability.servers.execution"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
_EXECUTABLE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_SHELL_META = frozenset(";&|`$><\r\n\x00")
_CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"


class ExecutionError(ValueError):
    """Stable, non-secret execution policy error."""


class ExecutionPolicy(BaseModel):
    """Operator-owned Docker sandbox policy for exactly one run and asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_ref: str
    engagement_id: str
    run_id: str
    workspace_root: str
    image: str
    allowed_executables: dict[str, str] = Field(min_length=1)
    writable: bool = False
    max_timeout_seconds: float = Field(default=30.0, gt=0, le=300.0)
    max_output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    tmpfs_bytes: int = Field(default=16_777_216, ge=1_048_576, le=67_108_864)
    pids_limit: int = Field(default=64, ge=8, le=256)
    memory: str = Field(default="256m", pattern=r"^[1-9][0-9]{0,3}[mMgG]$")
    cpus: float = Field(default=0.5, gt=0, le=2.0)
    container_user: str = Field(default="65532:65532", pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")
    docker_binary: str = "docker"

    @field_validator("asset_ref", "engagement_id", "run_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("execution identities must be bounded opaque identifiers")
        return value

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if not _IMAGE_DIGEST.fullmatch(value):
            raise ValueError("execution image must be lowercase and pinned by sha256 digest")
        return value

    @field_validator("allowed_executables")
    @classmethod
    def valid_allowlist(cls, value: dict[str, str]) -> dict[str, str]:
        forbidden = {"sh", "bash", "dash", "zsh", "cmd", "powershell", "python", "perl", "ruby"}
        for name, container_path in value.items():
            if (
                not _EXECUTABLE.fullmatch(name)
                or name in forbidden
                or not container_path.startswith("/usr/bin/")
                or PurePosixPath(container_path).name != name
                or ".." in PurePosixPath(container_path).parts
            ):
                raise ValueError("execution allowlist contains an unsafe executable mapping")
        return dict(value)

    @field_validator("docker_binary")
    @classmethod
    def valid_docker_binary(cls, value: str) -> str:
        if not value or any(character in value for character in _SHELL_META):
            raise ValueError("docker_binary must be one executable path")
        return value

    @model_validator(mode="after")
    def workspace_is_narrow(self) -> ExecutionPolicy:
        root = Path(self.workspace_root)
        if not root.is_absolute() or root == Path(root.anchor) or root == Path.home():
            raise ValueError("workspace_root must be a narrow absolute path")
        return self

    @classmethod
    def load(cls, path: str) -> ExecutionPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class CommandResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int


ProcessFactory = Callable[..., Awaitable[Any]]


def _safe_host_environment() -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", "")}
    environment["LANG"] = os.environ.get("LANG", "C.UTF-8")
    return environment


def _validate_argument(argument: str) -> None:
    if not isinstance(argument, str) or len(argument) > 1024:
        raise ExecutionError("execution_invalid_argument: arguments must be bounded strings")
    if any(character in argument for character in _SHELL_META):
        raise ExecutionError("execution_shell_syntax_forbidden")
    if argument.startswith(('/', '\\')) or PureWindowsPath(argument).is_absolute():
        raise ExecutionError("execution_invalid_path: absolute paths are forbidden")
    normalized = argument.replace("\\", "/")
    if "\\" in argument or ".." in PurePosixPath(normalized).parts:
        raise ExecutionError("execution_invalid_path: traversal is forbidden")


class DockerExecutionEngine:
    def __init__(
        self,
        policy: ExecutionPolicy,
        *,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        cleanup_process_factory: ProcessFactory | None = None,
    ) -> None:
        self.policy = policy
        self._process_factory = process_factory
        self._cleanup_process_factory = cleanup_process_factory or process_factory

    def _check_context(
        self,
        asset_ref: str,
        engagement_id: str,
        run_id: str,
    ) -> None:
        if (asset_ref, engagement_id, run_id) != (
            self.policy.asset_ref,
            self.policy.engagement_id,
            self.policy.run_id,
        ):
            raise ExecutionError("execution_context_mismatch")

    def docker_arguments(
        self,
        *,
        container_name: str,
        executable: str,
        args: Sequence[str],
    ) -> tuple[str, ...]:
        container_executable = self.policy.allowed_executables.get(executable)
        if container_executable is None:
            raise ExecutionError("execution_executable_not_allowed")
        if len(args) > 64:
            raise ExecutionError("execution_invalid_argument: too many arguments")
        for argument in args:
            _validate_argument(argument)

        mount = (
            f"type=bind,src={self.policy.workspace_root},dst=/workspace"
            + ("" if self.policy.writable else ",readonly")
        )
        return (
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.policy.tmpfs_bytes}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.policy.container_user,
            "--pids-limit",
            str(self.policy.pids_limit),
            "--memory",
            self.policy.memory.lower(),
            "--cpus",
            str(self.policy.cpus),
            "--workdir",
            "/workspace",
            "--mount",
            mount,
            "--env",
            f"PATH={_CONTAINER_PATH}",
            "--env",
            "LANG=C.UTF-8",
            self.policy.image,
            container_executable,
            *args,
        )

    async def run_command(
        self,
        asset_ref: str,
        executable: str,
        args: Sequence[str],
        timeout_seconds: float,
        engagement_id: str,
        run_id: str,
    ) -> CommandResult:
        self._check_context(asset_ref, engagement_id, run_id)
        if not 0 < timeout_seconds <= self.policy.max_timeout_seconds:
            raise ExecutionError("execution_invalid_timeout")
        container_name = f"redteam-exec-{uuid.uuid4().hex}"
        docker_args = self.docker_arguments(
            container_name=container_name,
            executable=executable,
            args=args,
        )
        started = time.monotonic()
        process = await self._process_factory(
            self.policy.docker_binary,
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_host_environment(),
        )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_truncated = False
        stderr_truncated = False
        try:
            stdout_truncated, stderr_truncated = await asyncio.wait_for(
                self._collect_output(process, stdout_buffer, stderr_buffer),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._cleanup_container(process, container_name)
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            stdout, stderr, truncated = self._bounded_result(
                stdout_buffer,
                stderr_buffer,
                stdout_truncated or stderr_truncated,
            )
            return CommandResult(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                truncated=truncated,
                duration_ms=duration_ms,
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._cleanup_container(process, container_name))
            await asyncio.shield(cleanup)
            raise
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        stdout, stderr, truncated = self._bounded_result(
            stdout_buffer,
            stderr_buffer,
            stdout_truncated or stderr_truncated,
        )
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            truncated=truncated,
            duration_ms=duration_ms,
        )

    async def _collect_output(
        self,
        process: Any,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> tuple[bool, bool]:
        if process.stdout is None or process.stderr is None:
            raise ExecutionError("execution_process_stream_unavailable")
        stdout_task = asyncio.create_task(
            self._read_bounded(process.stdout, stdout_buffer)
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(process.stderr, stderr_buffer)
        )
        try:
            stdout_truncated, stderr_truncated, _ = await asyncio.gather(
                stdout_task,
                stderr_task,
                process.wait(),
            )
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
        return bool(stdout_truncated), bool(stderr_truncated)

    async def _read_bounded(self, stream: Any, output: bytearray) -> bool:
        truncated = False
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return truncated
            remaining = self.policy.max_output_bytes - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True

    def _bounded_result(
        self,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
        stream_truncated: bool,
    ) -> tuple[str, str, bool]:
        stdout = bytes(stdout_buffer[: self.policy.max_output_bytes])
        remaining = max(0, self.policy.max_output_bytes - len(stdout))
        stderr = bytes(stderr_buffer[:remaining])
        truncated = stream_truncated or len(stderr_buffer) > remaining
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            truncated,
        )

    async def _cleanup_container(self, process: Any, container_name: str) -> None:
        cleanup_succeeded = False
        try:
            cleanup = await self._cleanup_process_factory(
                self.policy.docker_binary,
                "rm",
                "--force",
                container_name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_safe_host_environment(),
            )
            cleanup_succeeded = await asyncio.wait_for(cleanup.wait(), timeout=5) == 0
        except (OSError, TimeoutError):
            cleanup_succeeded = False
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not cleanup_succeeded:
            raise ExecutionError("execution_container_cleanup_failed")


def ensure_runtime_available(policy: ExecutionPolicy) -> None:
    """Fail before serving when POSIX workspace isolation or Docker is unavailable."""

    if os.name != "posix":
        raise ExecutionError("execution_platform_unsupported: run under WSL2/Linux")
    root = Path(policy.workspace_root)
    if not root.is_dir() or root.is_symlink():
        raise ExecutionError("execution_workspace_unavailable")
    try:
        result = subprocess.run(
            [policy.docker_binary, "version", "--format", "{{.Server.Version}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_safe_host_environment(),
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError("execution_docker_unavailable") from exc
    if result.returncode != 0:
        raise ExecutionError("execution_docker_unavailable")


def create_server(
    policy: ExecutionPolicy,
    *,
    engine: DockerExecutionEngine | None = None,
) -> MCPServer:
    executor = engine or DockerExecutionEngine(policy)
    server = MCPServer("execution-substrate", log_level="WARNING")

    async def run_command(
        asset_ref: str,
        executable: str,
        args: list[str],
        timeout_seconds: float = policy.max_timeout_seconds,
        engagement_id: str = "",
        run_id: str = "",
    ) -> CommandResult:
        """Proposed: run one allowlisted argv command in a locked-down Docker container."""

        return await executor.run_command(
            asset_ref,
            executable,
            args,
            timeout_seconds,
            engagement_id,
            run_id,
        )

    run_command.__annotations__["asset_ref"] = Literal[policy.asset_ref]
    run_command.__annotations__["args"] = Annotated[list[str], Field(max_length=64)]
    run_command.__annotations__["timeout_seconds"] = Annotated[
        float,
        Field(gt=0, le=policy.max_timeout_seconds),
    ]
    server.tool()(run_command)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    policy = ExecutionPolicy.load(args.policy)
    ensure_runtime_available(policy)
    create_server(policy).run(transport="stdio")


if __name__ == "__main__":
    main()
