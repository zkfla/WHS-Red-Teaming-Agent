"""Docker-only, run-scoped command execution MCP for approved workspaces.

The server never invokes a shell and never falls back to host execution.
Operator policy fixes one asset, engagement, run, workspace, image digest,
and exact executable allowlist before the MCP process starts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROPOSED_OPERATION = "command_run"
REMOTE_TOOL_NAME = "run_command"
MODULE = "redteam.capability.servers.execution"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
_EXECUTABLE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_ERROR_CODE = re.compile(r"^execution_[a-z0-9_]+$")
_SHELL_META = frozenset(";&|`$><\r\n\x00")
_CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"
_MANAGED_LABEL = "io.whs-redteam.execution.managed=true"
_CONTEXT_LABEL_KEY = "io.whs-redteam.execution.context"

# Options are deliberately enumerated.  Values following options such as
# ``head -c 1024`` are still validated by the generic argument/path checks.
_DEFAULT_ALLOWED_OPTIONS: dict[str, frozenset[str]] = {
    "grep": frozenset(
        {
            "--",
            "-E",
            "-F",
            "-H",
            "-h",
            "-i",
            "-l",
            "-L",
            "-n",
            "-q",
            "-s",
            "-v",
            "-w",
            "-x",
            "--count",
            "--extended-regexp",
            "--fixed-strings",
            "--ignore-case",
            "--invert-match",
            "--line-number",
            "--no-messages",
            "--quiet",
            "--regexp",
            "--word-regexp",
        }
    ),
    "sort": frozenset(
        {
            "--",
            "-b",
            "-d",
            "-f",
            "-g",
            "-i",
            "-M",
            "-n",
            "-R",
            "-r",
            "-s",
            "-u",
            "-V",
            "-z",
            "--dictionary-order",
            "--general-numeric-sort",
            "--ignore-case",
            "--ignore-leading-blanks",
            "--month-sort",
            "--numeric-sort",
            "--random-sort",
            "--reverse",
            "--stable",
            "--unique",
            "--version-sort",
            "--zero-terminated",
        }
    ),
    "uniq": frozenset(
        {
            "--",
            "-c",
            "-d",
            "-D",
            "-i",
            "-u",
            "-z",
            "--all-repeated",
            "--count",
            "--ignore-case",
            "--repeated",
            "--unique",
            "--zero-terminated",
        }
    ),
    "wc": frozenset(
        {
            "--",
            "-c",
            "-L",
            "-l",
            "-m",
            "-w",
            "--bytes",
            "--chars",
            "--lines",
            "--max-line-length",
            "--words",
        }
    ),
    "head": frozenset({"--", "-c", "-n", "-q", "-v", "--bytes", "--lines", "--quiet", "--verbose"}),
    "tail": frozenset(
        {
            "--",
            "-c",
            "-f",
            "-n",
            "-q",
            "-v",
            "--bytes",
            "--follow",
            "--lines",
            "--quiet",
            "--verbose",
        }
    ),
    "cut": frozenset(
        {
            "--",
            "-b",
            "-c",
            "-d",
            "-f",
            "-s",
            "-z",
            "--bytes",
            "--characters",
            "--delimiter",
            "--fields",
            "--only-delimited",
            "--output-delimiter",
            "--zero-terminated",
        }
    ),
    "jq": frozenset(
        {
            "--",
            "-c",
            "-e",
            "-j",
            "-M",
            "-n",
            "-R",
            "-r",
            "-S",
            "-s",
            "--ascii-output",
            "--compact-output",
            "--exit-status",
            "--join-output",
            "--monochrome-output",
            "--null-input",
            "--raw-input",
            "--raw-output",
            "--slurp",
            "--sort-keys",
            "--tab",
        }
    ),
}
_DANGEROUS_OPTIONS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o", "--output", "--compress-program", "--files0-from"}),
    "jq": frozenset(
        {
            "-f",
            "-L",
            "--argfile",
            "--from-file",
            "--library-path",
            "--rawfile",
            "--slurpfile",
        }
    ),
    "grep": frozenset({"-f", "--file"}),
}


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
    allowed_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    writable: bool = False
    max_timeout_seconds: float = Field(default=30.0, gt=0, le=300.0)
    max_output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    tmpfs_bytes: int = Field(default=16_777_216, ge=1_048_576, le=67_108_864)
    pids_limit: int = Field(default=64, ge=8, le=256)
    memory: str = Field(default="256m", pattern=r"^[1-9][0-9]{0,3}[mMgG]$")
    cpus: float = Field(default=0.5, gt=0, le=2.0)
    container_user: str = Field(default="65532:65532", pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")
    docker_binary: str = "docker"
    max_jobs: int = Field(default=32, ge=1, le=256)
    completed_job_ttl_seconds: float = Field(default=300.0, ge=1.0, le=86_400.0)

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

    @field_validator("allowed_options")
    @classmethod
    def valid_option_allowlist(
        cls,
        value: dict[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]]:
        for executable, options in value.items():
            safe = _DEFAULT_ALLOWED_OPTIONS.get(executable)
            if safe is None or any(option not in safe for option in options):
                raise ValueError("execution option allowlist contains an unsafe option")
        return {name: tuple(options) for name, options in value.items()}

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
        if set(self.allowed_options) - set(self.allowed_executables):
            raise ValueError("execution option allowlist names an unavailable executable")
        return self

    def options_for(self, executable: str) -> frozenset[str]:
        configured = self.allowed_options.get(executable)
        if configured is not None:
            return frozenset(configured)
        return _DEFAULT_ALLOWED_OPTIONS.get(executable, frozenset())

    @classmethod
    def load(
        cls,
        path: str,
        *,
        expected_sha256: str | None = None,
    ) -> ExecutionPolicy:
        content = Path(path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ExecutionError("execution_policy_digest_mismatch")
        return cls.model_validate(json.loads(content))


def load_execution_policy(
    path: str,
    expected_sha256: str | None = None,
) -> tuple[ExecutionPolicy, str]:
    content = Path(path).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ExecutionError("execution_policy_digest_mismatch")
    return ExecutionPolicy.model_validate(json.loads(content)), digest


class CommandResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int


class JobStartResult(BaseModel):
    job_id: str
    status: Literal["running"] = "running"


class JobStatusResult(BaseModel):
    job_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    result: CommandResult | None = None
    error: str | None = None


class JobCancelResult(BaseModel):
    job_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    cancelled: bool


@dataclass
class _JobRecord:
    asset_ref: str
    engagement_id: str
    run_id: str
    updated_at: float
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    task: asyncio.Task[None] | None = None
    result: CommandResult | None = None
    error: str | None = None


ProcessFactory = Callable[..., Awaitable[Any]]


@dataclass
class _OutputBudget:
    remaining: int
    peak_buffered: int = 0
    truncated: bool = False


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


def _option_name(argument: str) -> str:
    return argument.split("=", 1)[0]


def _validate_options(policy: ExecutionPolicy, executable: str, args: Sequence[str]) -> None:
    allowed = policy.options_for(executable)
    dangerous = _DANGEROUS_OPTIONS.get(executable, frozenset())
    options_ended = False
    for argument in args:
        if options_ended or not argument.startswith("-") or argument == "-":
            continue
        option = _option_name(argument)
        if option == "--":
            options_ended = True
            continue
        if option in dangerous:
            raise ExecutionError("execution_dangerous_option_forbidden")
        if option not in allowed:
            raise ExecutionError("execution_option_not_allowed")


def _context_label(policy: ExecutionPolicy) -> str:
    identity = "\0".join(
        (policy.asset_ref, policy.engagement_id, policy.run_id, policy.workspace_root)
    )
    return f"{_CONTEXT_LABEL_KEY}={hashlib.sha256(identity.encode()).hexdigest()}"


def _execution_failure(exc: ExecutionError) -> Any:
    code = str(exc).partition(":")[0]
    if not _ERROR_CODE.fullmatch(code):
        code = "execution_request_rejected"
    return CallToolResult(
        content=[TextContent(type="text", text="execution request rejected")],
        structured_content={"error_code": code},
        is_error=True,
    )


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
        self._jobs: dict[str, _JobRecord] = {}
        self._jobs_lock = asyncio.Lock()
        self._last_peak_output_bytes = 0

    @property
    def last_peak_output_bytes(self) -> int:
        """Largest retained stdout/stderr byte total for the last command."""

        return self._last_peak_output_bytes

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
        _validate_options(self.policy, executable, args)

        mount = (
            f"type=bind,src={self.policy.workspace_root},dst=/workspace"
            + ("" if self.policy.writable else ",readonly")
        )
        return (
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            _MANAGED_LABEL,
            "--label",
            _context_label(self.policy),
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
        budget = _OutputBudget(remaining=self.policy.max_output_bytes)
        try:
            stdout_truncated, stderr_truncated = await asyncio.wait_for(
                self._collect_output(process, stdout_buffer, stderr_buffer, budget),
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
        finally:
            self._last_peak_output_bytes = budget.peak_buffered
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

    async def job_start(
        self,
        asset_ref: str,
        executable: str,
        args: Sequence[str],
        timeout_seconds: float,
        engagement_id: str,
        run_id: str,
    ) -> JobStartResult:
        self._check_context(asset_ref, engagement_id, run_id)
        if not 0 < timeout_seconds <= self.policy.max_timeout_seconds:
            raise ExecutionError("execution_invalid_timeout")
        self.docker_arguments(
            container_name="redteam-exec-validation",
            executable=executable,
            args=args,
        )
        job_id = f"job-{uuid.uuid4().hex}"
        async with self._jobs_lock:
            self._prune_jobs_locked()
            if len(self._jobs) >= self.policy.max_jobs:
                raise ExecutionError("execution_job_capacity_reached")
            record = _JobRecord(
                asset_ref=asset_ref,
                engagement_id=engagement_id,
                run_id=run_id,
                updated_at=time.monotonic(),
            )
            self._jobs[job_id] = record
            record.task = asyncio.create_task(
                self._run_job(
                    job_id,
                    asset_ref,
                    executable,
                    tuple(args),
                    timeout_seconds,
                    engagement_id,
                    run_id,
                )
            )
        return JobStartResult(job_id=job_id)

    async def job_status(
        self,
        asset_ref: str,
        job_id: str,
        engagement_id: str,
        run_id: str,
    ) -> JobStatusResult:
        self._check_context(asset_ref, engagement_id, run_id)
        async with self._jobs_lock:
            self._prune_jobs_locked()
            record = self._owned_job(job_id, asset_ref, engagement_id, run_id)
            return JobStatusResult(
                job_id=job_id,
                status=record.status,
                result=record.result,
                error=record.error,
            )

    async def job_cancel(
        self,
        asset_ref: str,
        job_id: str,
        engagement_id: str,
        run_id: str,
    ) -> JobCancelResult:
        self._check_context(asset_ref, engagement_id, run_id)
        async with self._jobs_lock:
            self._prune_jobs_locked()
            record = self._owned_job(job_id, asset_ref, engagement_id, run_id)
            task = record.task if record.status == "running" else None
        if task is not None:
            task.cancel()
            await asyncio.shield(task)
        async with self._jobs_lock:
            record = self._owned_job(job_id, asset_ref, engagement_id, run_id)
            return JobCancelResult(
                job_id=job_id,
                status=record.status,
                cancelled=task is not None and record.status == "cancelled",
            )

    async def _run_job(
        self,
        job_id: str,
        asset_ref: str,
        executable: str,
        args: Sequence[str],
        timeout_seconds: float,
        engagement_id: str,
        run_id: str,
    ) -> None:
        try:
            result = await self.run_command(
                asset_ref,
                executable,
                args,
                timeout_seconds,
                engagement_id,
                run_id,
            )
        except asyncio.CancelledError:
            async with self._jobs_lock:
                record = self._jobs.get(job_id)
                if record is not None:
                    record.status = "cancelled"
                    record.updated_at = time.monotonic()
            return
        except Exception as exc:
            error = str(exc) if isinstance(exc, ExecutionError) else "execution_job_failed"
            async with self._jobs_lock:
                record = self._jobs.get(job_id)
                if record is not None:
                    record.status = "failed"
                    record.error = error
                    record.updated_at = time.monotonic()
            return
        async with self._jobs_lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.status = "completed"
                record.result = result
                record.updated_at = time.monotonic()

    def _owned_job(
        self,
        job_id: str,
        asset_ref: str,
        engagement_id: str,
        run_id: str,
    ) -> _JobRecord:
        record = self._jobs.get(job_id)
        if record is None or (
            record.asset_ref,
            record.engagement_id,
            record.run_id,
        ) != (asset_ref, engagement_id, run_id):
            raise ExecutionError("execution_job_not_found")
        return record

    def _prune_jobs_locked(self) -> None:
        now = time.monotonic()
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.status != "running"
            and now - record.updated_at >= self.policy.completed_job_ttl_seconds
        ]
        for job_id in expired:
            del self._jobs[job_id]
        completed = sorted(
            (
                (record.updated_at, job_id)
                for job_id, record in self._jobs.items()
                if record.status != "running"
            )
        )
        while len(self._jobs) >= self.policy.max_jobs and completed:
            _, job_id = completed.pop(0)
            del self._jobs[job_id]

    async def _collect_output(
        self,
        process: Any,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
        budget: _OutputBudget,
    ) -> tuple[bool, bool]:
        if process.stdout is None or process.stderr is None:
            raise ExecutionError("execution_process_stream_unavailable")
        stdout_task = asyncio.create_task(
            self._read_bounded(process.stdout, stdout_buffer, budget)
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(process.stderr, stderr_buffer, budget)
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

    async def _read_bounded(
        self,
        stream: Any,
        output: bytearray,
        budget: _OutputBudget,
    ) -> bool:
        truncated = False
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return truncated
            accepted = min(len(chunk), budget.remaining)
            if accepted:
                output.extend(chunk[:accepted])
                budget.remaining -= accepted
                budget.peak_buffered = max(
                    budget.peak_buffered,
                    self.policy.max_output_bytes - budget.remaining,
                )
            if accepted < len(chunk):
                truncated = True
                budget.truncated = True

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


def cleanup_orphaned_containers(policy: ExecutionPolicy) -> None:
    """Remove containers left by a dead server for this exact run context."""

    filters = [
        "--filter",
        f"label={_MANAGED_LABEL}",
        "--filter",
        f"label={_context_label(policy)}",
    ]
    try:
        listed = subprocess.run(
            [policy.docker_binary, "ps", "-aq", *filters],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_safe_host_environment(),
            timeout=5,
            check=False,
        )
        identifiers = listed.stdout.decode("ascii", errors="ignore").split()
        if listed.returncode != 0:
            raise ExecutionError("execution_orphan_cleanup_failed")
        if not identifiers:
            return
        removed = subprocess.run(
            [policy.docker_binary, "rm", "--force", *identifiers],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_safe_host_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError("execution_orphan_cleanup_failed") from exc
    if removed.returncode != 0:
        raise ExecutionError("execution_orphan_cleanup_failed")


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

        try:
            return await executor.run_command(
                asset_ref,
                executable,
                args,
                timeout_seconds,
                engagement_id,
                run_id,
            )
        except ExecutionError as exc:
            return _execution_failure(exc)

    async def job_start(
        asset_ref: str,
        executable: str,
        args: list[str],
        timeout_seconds: float = policy.max_timeout_seconds,
        engagement_id: str = "",
        run_id: str = "",
    ) -> JobStartResult:
        """Proposed: start one allowlisted Docker command and return its job ID."""

        try:
            return await executor.job_start(
                asset_ref,
                executable,
                args,
                timeout_seconds,
                engagement_id,
                run_id,
            )
        except ExecutionError as exc:
            return _execution_failure(exc)

    async def job_status(
        asset_ref: str,
        job_id: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> JobStatusResult:
        """Proposed: inspect one job owned by the trusted asset and run context."""

        try:
            return await executor.job_status(asset_ref, job_id, engagement_id, run_id)
        except ExecutionError as exc:
            return _execution_failure(exc)

    async def job_cancel(
        asset_ref: str,
        job_id: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> JobCancelResult:
        """Proposed: cancel one running job and wait for container cleanup."""

        try:
            return await executor.job_cancel(asset_ref, job_id, engagement_id, run_id)
        except ExecutionError as exc:
            return _execution_failure(exc)

    run_command.__annotations__["asset_ref"] = Literal[policy.asset_ref]
    run_command.__annotations__["args"] = Annotated[list[str], Field(max_length=64)]
    run_command.__annotations__["timeout_seconds"] = Annotated[
        float,
        Field(gt=0, le=policy.max_timeout_seconds),
    ]
    job_start.__annotations__["asset_ref"] = Literal[policy.asset_ref]
    job_start.__annotations__["args"] = Annotated[list[str], Field(max_length=64)]
    job_start.__annotations__["timeout_seconds"] = Annotated[
        float,
        Field(gt=0, le=policy.max_timeout_seconds),
    ]
    job_status.__annotations__["asset_ref"] = Literal[policy.asset_ref]
    job_cancel.__annotations__["asset_ref"] = Literal[policy.asset_ref]
    for function in (run_command, job_start, job_status, job_cancel):
        server.tool()(function)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    policy = ExecutionPolicy.load(args.policy, expected_sha256=args.sha256)
    ensure_runtime_available(policy)
    cleanup_orphaned_containers(policy)
    create_server(policy).run(transport="stdio")


if __name__ == "__main__":
    main()
