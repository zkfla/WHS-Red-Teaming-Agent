from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from .capability.registry import CapabilityRegistry


@dataclass(frozen=True)
class SkillSpec:
    name: str
    purpose: str
    tools: tuple[str, ...]
    completion_criteria: tuple[str, ...]
    stop_conditions: tuple[str, ...]


class SkillRegistry:
    """Runtime-extensible skill catalog with no target-specific behavior."""

    def __init__(self, skills: Iterable[SkillSpec] = ()) -> None:
        self._skills = {skill.name: skill for skill in skills}

    def register(self, skill: SkillSpec, registry: CapabilityRegistry) -> None:
        validate_skill(skill, registry)
        self._skills[skill.name] = skill

    def resolve(self, names: Iterable[str] = ()) -> tuple[SkillSpec, ...]:
        selected = tuple(names) or tuple(self._skills)
        missing = [name for name in selected if name not in self._skills]
        if missing:
            raise KeyError(f"skills are not registered: {', '.join(missing)}")
        return tuple(self._skills[name] for name in selected)

    @classmethod
    def from_json(cls, path: Path, registry: CapabilityRegistry) -> "SkillRegistry":
        """Load operator-authored skill metadata, never executable code."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("skills", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError("skill manifest must contain a list of skills")
        result = cls()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("skill manifest entries must be objects")
            skill = SkillSpec(
                name=str(raw["name"]),
                purpose=str(raw.get("purpose", "")),
                tools=tuple(str(item) for item in raw.get("tools", ())),
                completion_criteria=tuple(str(item) for item in raw.get("completion_criteria", ())),
                stop_conditions=tuple(str(item) for item in raw.get("stop_conditions", ())),
            )
            result.register(skill, registry)
        return result


def default_skills() -> tuple[SkillSpec, ...]:
    return (
        SkillSpec(
            "network-recon",
            "Discover authorized hosts, open ports, and services.",
            ("python-port-recon", "dnsx"),
            ("All discovered assets are normalized into Asset Graph.", "Raw tool output is stored as evidence."),
            ("Target leaves authorized scope.", "Rate or approval policy blocks the action."),
        ),
        SkillSpec(
            "web-surface-recon",
            "Probe authorized HTTP services and identify web attack surface.",
            ("httpx", "whatweb"),
            ("Live services and technology observations are recorded.",),
            ("Non-authorized redirect or excessive request rate."),
        ),
        SkillSpec(
            "template-vulnerability-scan",
            "Run explicitly approved, template-based checks and create candidates.",
            ("nuclei",),
            ("Every candidate has the template, raw output, and target asset."),
            ("Approval is missing.", "A template requests a destructive action."),
        ),
        SkillSpec(
            "content-discovery",
            "Discover authorized web content using a bounded wordlist.",
            ("ffuf",),
            ("Results are deduplicated and imported as endpoint assets."),
            ("Approval is missing.", "Request budget is exceeded."),
        ),
    )


def validate_skill(skill: SkillSpec, registry: CapabilityRegistry) -> None:
    registry.resolve(skill.tools)
