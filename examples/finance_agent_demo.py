"""Run the real Agent loop against the local deliberately vulnerable bank lab."""

from __future__ import annotations

import asyncio
import os

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import load_app_config


async def main() -> None:
    os.chdir("/tmp")
    config = load_app_config("/app/demo.yaml")
    engagement, deps = await build_application(
        config,
        evidence_dir="/tmp/evidence",
        audit_dir="/tmp/audit",
        approval_dir="/tmp/approvals",
    )

    async def show(event) -> None:
        if event.event_type in {
            "RunStarted", "AgentStarted", "ActionProposed", "ToolExecutionFinished",
            "EvidenceCreated", "HypothesisCreated", "HypothesisResolved",
            "GraphUpdated", "FindingCreated", "FindingVerified", "ReportProjected",
            "RunFinished",
        }:
            print(f"EVENT {event.event_type}: {event.payload}")

    await deps.event_bus.subscribe("*", show)
    final = await run_engagement(engagement, deps=deps, orchestration_config=config.orchestration)
    graph = AttackGraphService(deps.graph_store)
    findings = await graph.list_findings(verified=True)
    hypotheses = await graph.list_hypotheses()
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    nodes = await graph.list_nodes()

    print("\n=== MENTORING DEMO RESULT ===")
    print(f"RunId: {final.run_id}")
    print(f"Termination: {final.termination_reason}")
    print(f"Actions executed: {final.actions_executed}")
    print(f"Graph nodes: {len(nodes)}")
    print(f"Evidence records: {len(evidence)}")
    print(f"Hypotheses: {len(hypotheses)}")
    print(f"Verified findings: {len(findings)}")
    for finding in findings:
        print(
            f"FINDING severity={finding.severity} verified={finding.verified} "
            f"evidence={len(finding.evidence_ids)} title={finding.title}"
        )

    if not findings:
        raise SystemExit("demo failed: expected a verified access-control finding")


if __name__ == "__main__":
    asyncio.run(main())
