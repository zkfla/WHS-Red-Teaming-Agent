from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from ..models import Scope


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname or value).lower().rstrip(".")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    executable: str
    purpose: str
    risk: str
    requires_approval: bool = False
    capabilities: tuple[str, ...] = ()


@dataclass
class CapabilityRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or not spec.executable:
            raise ValueError("tool name and executable are required")
        if any(token in spec.executable for token in (";", "&", "|", "<", ">", "$", "`", "\n", "\r")):
            raise ValueError("tool executable must be a single executable token")
        if spec.risk not in {"low", "medium", "high"}:
            raise ValueError("tool risk must be low, medium, or high")
        self.tools[spec.name] = spec

    @classmethod
    def from_json(cls, path: Path, base: "CapabilityRegistry | None" = None) -> "CapabilityRegistry":
        """Load operator-selected tool metadata, not arbitrary shell commands."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("tools", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError("tool manifest must contain a list of tools")
        registry = cls(dict((base or cls()).tools))
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("tool manifest entries must be objects")
            registry.register(ToolSpec(
                name=str(raw["name"]),
                executable=str(raw["executable"]),
                purpose=str(raw.get("purpose", "")),
                risk=str(raw.get("risk", "medium")),
                requires_approval=bool(raw.get("requires_approval", True)),
                capabilities=tuple(str(item) for item in raw.get("capabilities", ())),
            ))
        return registry

    def resolve(self, names: Iterable[str]) -> list[ToolSpec]:
        names = tuple(names)
        missing = [name for name in names if name not in self.tools]
        if missing:
            raise KeyError(f"tools are not registered: {', '.join(missing)}")
        return [self.tools[name] for name in names]

    def allowed_for(self, names: Iterable[str], scope: Scope) -> list[ToolSpec]:
        """Resolve configured tools; execution and approval remain Control Plane duties."""
        resolved = self.resolve(names)
        allowed_hosts = {_host(value) for value in scope.allowed_hosts}
        if not allowed_hosts:
            raise ValueError("a non-empty authorized scope is required")
        return resolved


def default_recon_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(ToolSpec("python-port-recon", "built-in", "bounded TCP port discovery", "low", capabilities=("network.recon",)))
    registry.register(ToolSpec("dnsx", "dnsx", "DNS resolution and record discovery", "low", capabilities=("dns.recon",)))
    registry.register(ToolSpec("httpx", "httpx", "HTTP probing and technology metadata", "low", capabilities=("http.recon",)))
    registry.register(ToolSpec("whatweb", "whatweb", "web technology fingerprinting", "low", capabilities=("http.recon",)))
    registry.register(ToolSpec("nuclei", "nuclei", "template-based vulnerability checks", "medium", True, ("vulnerability.scan",)))
    registry.register(ToolSpec("ffuf", "ffuf", "content discovery", "medium", True, ("content.discovery",)))
    registry.register(ToolSpec("playwright", "playwright", "browser-based workflow verification", "medium", True, ("browser.verify",)))
    return registry
