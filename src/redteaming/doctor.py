from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import shutil
import sys

from .capability.registry import CapabilityRegistry, default_recon_registry


@dataclass(frozen=True)
class Diagnostic:
    name: str
    status: str
    detail: str


def run_diagnostics(registry: CapabilityRegistry | None = None) -> tuple[Diagnostic, ...]:
    checks: list[Diagnostic] = []
    checks.append(Diagnostic(
        "python", "ok" if sys.version_info >= (3, 10) else "error",
        ".".join(str(item) for item in sys.version_info[:3]),
    ))
    checks.append(Diagnostic("redteaming", "ok", "package import path is active"))
    checks.append(Diagnostic(
        "litellm", "ok" if importlib.util.find_spec("litellm") else "optional",
        "LLM provider adapter available" if importlib.util.find_spec("litellm")
        else "install the llm extra for LiteLLM-backed planning",
    ))
    checks.append(Diagnostic(
        "playwright", "ok" if importlib.util.find_spec("playwright") else "optional",
        "browser session handoff available" if importlib.util.find_spec("playwright")
        else "install the browser extra only when browser handoff is needed",
    ))
    checks.append(Diagnostic(
        "docker", "ok" if shutil.which("docker") else "optional",
        "Docker testbed commands available" if shutil.which("docker")
        else "install Docker Desktop only for container testbeds",
    ))
    active_registry = registry or default_recon_registry()
    for spec in active_registry.tools.values():
        if spec.executable == "built-in":
            continue
        installed = shutil.which(spec.executable) is not None
        checks.append(Diagnostic(
            f"tool:{spec.name}", "ok" if installed else "optional",
            f"{spec.executable} is available" if installed
            else f"install {spec.executable} separately before using this capability",
        ))
    return tuple(checks)
