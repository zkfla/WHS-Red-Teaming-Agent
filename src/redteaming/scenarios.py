from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AttackScenario:
    """Declarative threat-emulation metadata, not an execution recipe."""

    id: str
    name: str
    objective: str
    tactics: tuple[str, ...]
    techniques: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    requires_identity: bool = False


DEFAULT_SCENARIOS: tuple[AttackScenario, ...] = (
    AttackScenario(
        id="external-exposure",
        name="External exposure assessment",
        objective="Identify reachable services and safely verify evidence-backed web exposure conditions.",
        tactics=("reconnaissance",),
        techniques=("T1595", "T1046", "T1595.002"),
        allowed_actions=("asset_discovery", "service_identification", "http_surface_identification",
                         "detector_analysis", "verification", "reporting"),
    ),
    AttackScenario(
        id="authenticated-web",
        name="Authenticated web authorization assessment",
        objective="Compare operator-supplied identities to verify object-level authorization differences.",
        tactics=("credential-access", "discovery"),
        techniques=("T1078", "T1087"),
        allowed_actions=("asset_discovery", "service_identification", "http_surface_identification",
                         "detector_analysis", "verification", "reporting"),
        requires_identity=True,
    ),
)


class ScenarioRegistry:
    def __init__(self, scenarios: tuple[AttackScenario, ...] = DEFAULT_SCENARIOS) -> None:
        self._items = {item.id: item for item in scenarios}

    def get(self, scenario_id: str) -> AttackScenario:
        return self._items.get(scenario_id, self._items["external-exposure"])

    def all(self) -> tuple[AttackScenario, ...]:
        return tuple(self._items.values())


_TECHNIQUE_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")


def valid_technique_ids(values: tuple[str, ...]) -> bool:
    return all(bool(_TECHNIQUE_ID.fullmatch(value)) for value in values)
