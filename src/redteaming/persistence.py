from __future__ import annotations

"""Small, secret-free engagement snapshot store.

The snapshot is deliberately a plain JSON document so it can be inspected,
backed up, and recovered on Windows or Bash without a database service.  It
contains observations and audit metadata, never identity profiles or headers.
"""

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .events import Event
from .graphs import AssetGraph, EvidenceStore, ExplorationGraph, ExplorationNode
from .models import Asset, Evidence, Finding, FindingStatus
from .planning import EngagementPlan, PlanStep


class SnapshotError(ValueError):
    pass


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def save_snapshot(path: Path, assets: AssetGraph, evidence: EvidenceStore,
                  findings: list[Finding], exploration: ExplorationGraph,
                  events: list[Event], plan: EngagementPlan | None = None) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "assets": [_json_safe(asdict(item)) for item in assets.assets.values()],
        "evidence": [_json_safe(asdict(item)) for item in evidence.items.values()],
        "findings": [_json_safe(asdict(item)) for item in findings],
        "exploration": {
            "nodes": [_json_safe(asdict(item)) for item in exploration.nodes.values()],
            "edges": [_json_safe(item) for item in exploration.edges],
            "anchors": [_json_safe(item) for item in exploration.anchors],
        },
        "events": [{"name": item.name, "payload": _json_safe(item.payload)} for item in events],
        "plan": _json_safe(asdict(plan)) if plan is not None else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_snapshot(path: Path, assets: AssetGraph, evidence: EvidenceStore,
                  findings: list[Finding], exploration: ExplorationGraph) -> tuple[list[Event], EngagementPlan | None]:
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise SnapshotError("unsupported engagement snapshot version")
        for raw in payload.get("assets", []):
            assets.assets[str(raw["id"])] = Asset(**raw)
        for raw in payload.get("evidence", []):
            evidence.items[str(raw["id"])] = Evidence(**raw)
        for raw in payload.get("findings", []):
            raw = dict(raw)
            raw["status"] = FindingStatus(str(raw.get("status", FindingStatus.VALIDATED.value)))
            findings.append(Finding(**raw))
        graph = payload.get("exploration", {})
        for raw in graph.get("nodes", []):
            node = ExplorationNode(**raw)
            exploration.nodes[node.id] = node
        exploration.edges.extend(tuple(item) for item in graph.get("edges", []))
        exploration.anchors.extend(tuple(item) for item in graph.get("anchors", []))
        events = [Event(str(item["name"]), dict(item.get("payload", {})))
                  for item in payload.get("events", [])]
        raw_plan = payload.get("plan")
        plan = None
        if raw_plan:
            plan = EngagementPlan(
                objective=str(raw_plan["objective"]),
                scope_hosts=tuple(raw_plan["scope_hosts"]),
                steps=tuple(PlanStep(**step) for step in raw_plan["steps"]),
                stop_conditions=tuple(raw_plan["stop_conditions"]),
                source=str(raw_plan["source"]),
                id=str(raw_plan["id"]),
                scenario=str(raw_plan.get("scenario", "authorized exposure assessment")),
                scenario_id=str(raw_plan.get("scenario_id", "external-exposure")),
                scenario_tactics=tuple(raw_plan.get("scenario_tactics", ())),
                scenario_techniques=tuple(raw_plan.get("scenario_techniques", ())),
            )
        return events, plan
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"could not load engagement snapshot: {exc}") from exc
