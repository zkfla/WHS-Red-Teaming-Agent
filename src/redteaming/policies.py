"""Operator-configurable discovery policies.

The defaults are a bounded, safe baseline. They are deliberately not tied to
any fixture or target; an engagement can replace them with a JSON policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiscoveryPolicy:
    seed_paths: tuple[str, ...] = (
        "/robots.txt", "/sitemap.xml", "/openapi.json", "/swagger.json",
    )
    max_paths: int = 24
    follow_same_origin_links: bool = True
    inspect_api_specifications: bool = True
    canary_prefix: str = "redteam-canary"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "DiscoveryPolicy":
        paths = value.get("seed_paths", cls().seed_paths)
        return cls(
            seed_paths=tuple(str(item) for item in paths if str(item).startswith("/")),
            max_paths=max(1, min(int(value.get("max_paths", 24)), 256)),
            follow_same_origin_links=bool(value.get("follow_same_origin_links", True)),
            inspect_api_specifications=bool(value.get("inspect_api_specifications", True)),
            canary_prefix=str(value.get("canary_prefix", "redteam-canary"))[:48] or "redteam-canary",
        )


@dataclass(frozen=True)
class ReconPolicy:
    ports: tuple[int, ...] = tuple(range(1, 1025))
    timeout_seconds: float = 0.35
    max_ports: int = 2048

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ReconPolicy":
        ports = tuple(sorted({int(item) for item in value.get("ports", cls().ports)
                              if 1 <= int(item) <= 65535}))
        return cls(
            ports=ports or cls().ports,
            timeout_seconds=max(0.05, min(float(value.get("timeout_seconds", 0.35)), 10.0)),
            max_ports=max(1, min(int(value.get("max_ports", 2048)), 65535)),
        )


def load_policies(path: Path | None) -> tuple[DiscoveryPolicy, ReconPolicy]:
    if path is None or not path.exists():
        return DiscoveryPolicy(), ReconPolicy()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("policy file must contain a JSON object")
    discovery = data.get("discovery", {})
    recon = data.get("recon", {})
    return (DiscoveryPolicy.from_mapping(discovery if isinstance(discovery, dict) else {}),
            ReconPolicy.from_mapping(recon if isinstance(recon, dict) else {}))


def load_detector_names(path: Path | None) -> tuple[str, ...]:
    """Load an optional detector subset without making target-specific code."""
    if path is None or not path.exists():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("policy file must contain a JSON object")
    values = data.get("detectors", ())
    if not isinstance(values, (list, tuple)):
        raise ValueError("detectors must be a list")
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
