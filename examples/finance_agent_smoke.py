"""Exercise the default Agent loop through real MCP servers in the isolated lab.

First retain the shipped approval policy and assert Web waits. Then run an
explicit synthetic-only read test with auto-approval of its high-risk Web tool.
"""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason


async def exercise(*, require_approval: bool) -> None:
    config = load_app_config("/app/finance-app.yaml")
    assert config.engagement.scope.allowed_hosts == ["mock-bank"]
    assert config.llm.provider == "fake"
    if not require_approval:
        # The synthetic-only run opts into auto-approval; high-risk Web tools
        # remain gated by registry risk even when the explicit list is empty.
        config.engagement.approval_policy.require_approval_for = []
        config.engagement.approval_policy.auto_approve = True
    with TemporaryDirectory() as directory:
        async with AsyncExitStack() as stack:
            engagement, deps = await build_application(
                config, mcp_stack=stack,
                evidence_dir=Path(directory) / "evidence",
                audit_dir=Path(directory) / "audit",
                approval_dir=Path(directory) / "approvals",
                orchestration_settings=OrchestrationSettings(
                    checkpointing=False, max_steps=2
                ),
            )
            events: list[EventEnvelope] = []

            async def collect(event: EventEnvelope) -> None:
                events.append(event)

            await deps.event_bus.subscribe("*", collect)
            final = await run_engagement(engagement, deps=deps)
            started = [e.payload["tool_id"] for e in events
                       if e.event_type == EventType.TOOL_EXECUTION_STARTED]
            nodes = await AttackGraphService(deps.graph_store).list_nodes()
            print(
                f"TRACE: approval={require_approval} termination={final.termination_reason} "
                f"actions={final.actions_by_agent} tools={started} "
                f"nodes={[(str(node.id), node.type.value) for node in nodes]}",
                flush=True,
            )
            assert "recon.http_probe" in started, started
            assert not [e for e in events if e.event_type == EventType.TOOL_EXECUTION_FAILED]
            if require_approval:
                assert final.termination_reason == TerminationReason.APPROVAL_PENDING, final
                assert "web.http_request" not in started, started
            else:
                assert "web.http_request" in started, started
                assert final.actions_by_agent.get("web-api-agent", 0) >= 1
                assert final.actions_by_agent.get("recon-agent", 0) >= 1
    print(f"PASS: real Agent -> Control -> MCP -> Graph -> Web; approval={require_approval}")


async def main() -> None:
    await exercise(require_approval=True)
    await exercise(require_approval=False)


if __name__ == "__main__":
    asyncio.run(main())
