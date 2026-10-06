from __future__ import annotations

from dataclasses import asdict
from enum import Enum
import json

from .models import Asset, Evidence, Finding
from .graphs import ExplorationGraph
from .events import Event
from .planning import EngagementPlan


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    return str(value)


def engagement_payload(assets: list[Asset], evidence: list[Evidence],
                       findings: list[Finding] | None = None,
                       exploration: ExplorationGraph | None = None,
                       events: list[Event] | None = None,
                       plan: EngagementPlan | None = None) -> dict[str, object]:
    """Build a machine-readable, evidence-only engagement snapshot."""
    return {
        "scope": list(plan.scope_hosts) if plan is not None else [],
        "plan": asdict(plan) if plan is not None else None,
        "assets": [asdict(item) for item in assets],
        "evidence": [asdict(item) for item in evidence],
        "findings": [asdict(item) for item in (findings or [])],
        "exploration_graph": ({
            "nodes": asdict(exploration).get("nodes", {}) if exploration is not None else {},
            "edges": asdict(exploration).get("edges", []) if exploration is not None else [],
            "anchors": asdict(exploration).get("anchors", {}) if exploration is not None else {},
        }),
        "events": [asdict(item) for item in (events or [])],
    }


def render_engagement_json(assets: list[Asset], evidence: list[Evidence],
                           findings: list[Finding] | None = None,
                           exploration: ExplorationGraph | None = None,
                           events: list[Event] | None = None,
                           plan: EngagementPlan | None = None) -> str:
    return json.dumps(engagement_payload(assets, evidence, findings, exploration, events, plan),
                      ensure_ascii=False, indent=2, default=_json_default)


def render_markdown(finding: Finding) -> str:
    evidence = "\n".join(f"- `{item}`" for item in finding.evidence_ids)
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(finding.reproduction_steps, 1))
    return f"""# {finding.title}

- **Category:** {finding.category}
- **Severity:** {finding.severity}
- **Status:** {finding.status.value}
- **Affected assets:** {', '.join(finding.asset_ids)}
- **Root cause:** {finding.root_cause or 'not recorded'}
- **Verified candidate:** `{finding.candidate_id or 'legacy snapshot'}`

## Impact

{finding.impact}

## Reproduction

{steps}

## Evidence

{evidence}

## Remediation

{finding.remediation}
"""


def render_engagement_report(assets: list[Asset], evidence: list[Evidence],
                             findings: list[Finding] | None = None,
                             exploration: ExplorationGraph | None = None,
                             events: list[Event] | None = None,
                             plan: EngagementPlan | None = None) -> str:
    """Render an evidence-backed engagement snapshot without inventing findings."""
    graph_summary = "- graph not supplied"
    if exploration is not None:
        graph_summary = (
            f"- nodes: {len(exploration.nodes)}\n"
            f"- edges: {len(exploration.edges)}\n"
            f"- asset anchors: {len(exploration.anchors)}"
        )
    event_lines = "- no event history"
    if events:
        event_lines = "\n".join(
            f"- {event.name} " + " ".join(f"{key}={value}" for key, value in event.payload.items())
            for event in events
        )
    asset_lines = "\n".join(f"- `{asset.kind}` `{asset.canonical_value}`" for asset in assets) or "- 없음"
    evidence_lines = "\n".join(
        f"- `{item.kind}` {item.summary} (id: `{item.id}`)" for item in evidence
    ) or "- 없음"
    finding_lines = "\n\n".join(render_markdown(item) for item in (findings or [])) or "- 없음"
    scenario_lines = "- plan not supplied"
    scope_lines = "- scope not supplied"
    if plan is not None:
        techniques = sorted(set(plan.scenario_techniques) or {
            technique for step in plan.steps for technique in step.techniques
        })
        scope_lines = "\n".join(f"- `{host}`" for host in plan.scope_hosts) or "- empty"
        scenario_lines = (f"- id: `{plan.scenario_id}`\n"
                          f"- name: {plan.scenario}\n"
                          f"- source: {plan.source}\n"
                          f"- ATT&CK tactics: {', '.join(plan.scenario_tactics) or 'none'}\n"
                          f"- ATT&CK techniques: {', '.join(techniques) or 'none'}")
    return f"""# Redteaming Engagement Report

## Authorized Scope

{scope_lines}

## Engagement Scenario

{scenario_lines}

## Discovered Assets

{asset_lines}

## Evidence

{evidence_lines}

## Exploration Graph

{graph_summary}

## Execution Timeline

{event_lines}

## Finding Policy

No vulnerability is reported from reconnaissance alone. A Finding requires a detector candidate,
an explicit verification result, and reproducible evidence.

## Findings

{finding_lines}
"""
