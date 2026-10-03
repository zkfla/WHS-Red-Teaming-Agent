"""Run the REAL Agent loop with a REAL model against the local finance lab.

Unlike ``finance_agent_smoke.py`` this injects NO scripted stand-in model:
``build_application`` builds the real LLM client from the environment
(``REDTEAM_LLM_PROVIDER=litellm`` + ``REDTEAM_LLM_MODEL=chatgpt/<model>``),
so an actual GPT drives planning while the harness (Control + MCP tools)
performs every network action against the synthetic ``mock-bank`` target.

Prerequisites (see the run steps printed by the assistant):
  * finance-lab compose is up and its MCP ports are reachable at the
    ``REDTEAM_MCP_*_ENDPOINT`` values in the loaded env (host.env on the host).
  * ChatGPT OAuth token is cached (run ``examples/chatgpt_login.py`` once).

Env knobs:
  REDTEAM_FINANCE_APP_YAML  path to the app config (default: the repo's
                            deploy/mcp/finance/app.yaml).
  REDTEAM_AGENT_AUTO_APPROVE=1  relax the approval gate so the run completes
                            end-to-end instead of stopping at APPROVAL_PENDING
                            on the first high-risk Web tool. Leave unset to
                            exercise the real human-approval gate instead.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventEnvelope
from redteam.core.enums import RiskLevel
from redteam.orchestration.settings import OrchestrationSettings

_RENDERED = {
    "RunStarted", "AgentStarted", "ActionProposed", "ToolExecutionStarted",
    "ToolExecutionFinished", "ToolExecutionFailed", "EvidenceCreated",
    "HypothesisCreated", "HypothesisResolved", "FindingCreated",
    "FindingVerified", "OperatorApprovalRequested", "RunFinished",
}


def _config_path() -> str:
    override = os.environ.get("REDTEAM_FINANCE_APP_YAML")
    if override:
        return override
    return str(Path(__file__).parents[1] / "deploy" / "mcp" / "finance" / "app.yaml")


async def main() -> None:
    config = load_app_config(_config_path())

    # load_app_config only parses YAML, so apply the real model from the
    # environment here (the deploy yaml ships gpt-4o-mini as a placeholder).
    if os.environ.get("REDTEAM_LLM_PROVIDER"):
        config.llm.provider = os.environ["REDTEAM_LLM_PROVIDER"]
    if os.environ.get("REDTEAM_LLM_MODEL"):
        config.llm.default_model = os.environ["REDTEAM_LLM_MODEL"]
    if os.environ.get("REDTEAM_LLM_API_KEY_ENV_VAR"):
        config.llm.api_key_env_var = os.environ["REDTEAM_LLM_API_KEY_ENV_VAR"]

    # Optional: restrict the MCP server set (comma list) for this run, e.g. to
    # drop a server that is unavailable. Names must be a subset of the config.
    if os.environ.get("REDTEAM_MCP_SERVERS"):
        wanted = [s.strip() for s in os.environ["REDTEAM_MCP_SERVERS"].split(",") if s.strip()]
        config.capability.mcp_servers = [s for s in config.capability.mcp_servers if s in wanted]

    # The real-model builder rejects 'fake'; the model must come from the env.
    assert config.llm.provider == "litellm", config.llm.provider
    assert config.llm.default_model, "deployment must name a real model"
    print(
        f"CONFIG: provider={config.llm.provider} model={config.llm.default_model} "
        f"scope={config.engagement.scope.allowed_hosts} "
        f"mcp={config.capability.mcp_servers}",
        flush=True,
    )

    if os.environ.get("REDTEAM_AGENT_AUTO_APPROVE") in {"1", "true", "yes"}:
        # High-risk Web tools stay gated by registry risk; this only clears
        # the explicit require-approval list so the loop does not block on a
        # human approver that this non-interactive run does not have.
        config.engagement.approval_policy.require_approval_for = []
        config.engagement.approval_policy.auto_approve = True
        config.engagement.approval_policy.auto_approve_up_to = RiskLevel.HIGH
        print("APPROVAL: auto-approve up to HIGH (gate not exercised)", flush=True)
    else:
        print("APPROVAL: shipped policy (run will pause at the first gated tool)", flush=True)

    out = Path(os.environ.get("REDTEAM_RUN_DIR", ".realrun")).resolve()
    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            evidence_dir=out / "evidence",
            audit_dir=out / "audit",
            approval_dir=out / "approvals",
            orchestration_settings=OrchestrationSettings(
                checkpointing=False,
                max_steps=int(os.environ.get("REDTEAM_MAX_STEPS", "40")),
                # Real models sometimes need more than the default 3 admission
                # attempts per decision (the scripted stand-in passes in one).
                decision_max_attempts=int(os.environ.get("REDTEAM_DECISION_ATTEMPTS", "6")),
            ),
        )

        render_all = os.environ.get("REDTEAM_RENDER_ALL") in {"1", "true", "yes"}

        async def show(event: EventEnvelope) -> None:
            if render_all or event.event_type in _RENDERED:
                print(f"EVENT {event.event_type}: {event.payload}", flush=True)

        await deps.event_bus.subscribe("*", show)
        final = await run_engagement(
            engagement,
            deps=deps,
            orchestration_config=config.orchestration,
            objective=os.environ.get("REDTEAM_OBJECTIVE") or None,
        )

        graph = AttackGraphService(deps.graph_store)
        findings = await graph.list_findings(verified=True)
        hypotheses = await graph.list_hypotheses()
        evidence = await deps.evidence_store.list_for_run(final.run_id)
        nodes = await graph.list_nodes()

    print("\n=== REAL AGENT RUN RESULT ===", flush=True)
    print(f"RunId:            {final.run_id}")
    print(f"Termination:      {final.termination_reason}")
    print(f"Actions executed: {final.actions_executed}")
    print(f"By agent:         {final.actions_by_agent}")
    print(f"Graph nodes:      {len(nodes)}")
    print(f"Evidence records: {len(evidence)}")
    print(f"Hypotheses:       {len(hypotheses)}")
    print(f"Verified findings:{len(findings)}")
    for finding in findings:
        print(
            f"  FINDING severity={finding.severity} verified={finding.verified} "
            f"evidence={len(finding.evidence_ids)} title={finding.title}"
        )


if __name__ == "__main__":
    asyncio.run(main())
