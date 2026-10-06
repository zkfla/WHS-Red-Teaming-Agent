from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Iterable

from ..events import Event, EventBus
from ..models import Scope
from .registry import CapabilityRegistry


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Cross-platform runner: argv is passed directly, never through shell=True."""

    def run(self, argv: list[str], timeout: float = 120.0, env: dict[str, str] | None = None) -> ProcessResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged_env,
            shell=False,
        )
        return ProcessResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)


class CapabilityExecutionError(ValueError):
    pass


class CapabilityExecutor:
    """Control-plane gate for registered external tools.

    Adapters provide argv and explicit target strings; this class enforces
    registration, approval, and Scope before delegating to ProcessRunner.
    """

    def __init__(self, registry: CapabilityRegistry, runner: ProcessRunner | None = None,
                 events: EventBus | None = None) -> None:
        self.registry = registry
        self.runner = runner or ProcessRunner()
        self.events = events or EventBus()

    def execute(self, tool_name: str, argv: list[str], scope: Scope,
                targets: Iterable[str], approved: bool = False,
                timeout: float = 120.0) -> ProcessResult:
        specs = self.registry.allowed_for((tool_name,), scope)
        spec = specs[0]
        target_values = tuple(targets)
        if not target_values:
            raise CapabilityExecutionError("an explicit target is required")
        if any(not scope.allows(target) for target in target_values):
            raise CapabilityExecutionError("tool target is outside the authorized Scope")
        for token in argv[1:]:
            for candidate in _target_candidates(token):
                if not scope.allows(candidate):
                    raise CapabilityExecutionError(
                        f"argv contains a target outside the authorized Scope: {candidate}"
                    )
        if spec.requires_approval and not approved:
            raise CapabilityExecutionError(f"tool requires explicit approval: {tool_name}")
        if spec.executable != "built-in" and (not argv or argv[0] != spec.executable):
            raise CapabilityExecutionError("argv executable does not match the registered tool")
        self.events.publish(Event("capability.execution.started", {
            "tool": tool_name, "targets": target_values, "risk": spec.risk,
        }))
        try:
            result = self.runner.run(argv, timeout=timeout)
        except FileNotFoundError as exc:
            self.events.publish(Event("capability.execution.failed", {
                "tool": tool_name, "reason": "executable_not_found",
            }))
            raise CapabilityExecutionError(
                f"registered tool is not installed or not on PATH: {spec.executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            self.events.publish(Event("capability.execution.failed", {
                "tool": tool_name, "reason": "timeout",
            }))
            raise CapabilityExecutionError(f"tool execution timed out: {tool_name}") from exc
        self.events.publish(Event("capability.execution.completed", {
            "tool": tool_name, "returncode": result.returncode,
        }))
        return result


_HOST_TOKEN = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?$|"
    r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$|^localhost(?::\d{1,5})?$"
)


def _target_candidates(token: str) -> tuple[str, ...]:
    """Extract URL/host-shaped argv values without interpreting shell syntax."""
    values: list[str] = []
    for part in re.split(r"[,=]", token):
        value = part.strip().strip("'\"")
        if "://" in value or _HOST_TOKEN.fullmatch(value):
            values.append(value)
    return tuple(values)


def render_for_shell(argv: list[str], shell: str) -> str:
    """Render an audit-friendly command for bash or PowerShell without executing it."""
    normalized = shell.lower()
    if normalized in {"bash", "sh", "zsh"}:
        return shlex.join(argv)
    if normalized in {"powershell", "pwsh"}:
        escaped = [item.replace("'", "''") for item in argv]
        return " ".join(f"'{item}'" for item in escaped)
    raise ValueError(f"unsupported shell: {shell}")
