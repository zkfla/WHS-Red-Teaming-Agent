"""Runs the Minimal Vertical Slice programmatically (no CLI).

    python examples/minimal_run.py

Equivalent to ``python -m redteam.app.cli run examples/engagement.yaml``
but useful as a copy-pasteable starting point for team demos/tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from redteam.app.bootstrap import build_application, run_engagement
from redteam.capability.settings import mcp_server_ids_from_environment
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    LLMConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.contracts.events import EventEnvelope
from redteam.core.environment import env_snapshot

ENGAGEMENT_PATH = Path(__file__).parent / "engagement.yaml"


async def main() -> None:
    environment = env_snapshot()
    engagement, deps = await build_application(
        AppConfig(
            engagement=load_engagement_config(ENGAGEMENT_PATH),
            llm=LLMConfig.from_environment(environment),
            orchestration=OrchestrationConfig.from_environment(environment),
            capability=CapabilityConfig(
                mcp_servers=mcp_server_ids_from_environment(source=environment),
            ),
        )
    )

    events: list[str] = []

    async def _collect(event: EventEnvelope) -> None:
        events.append(event.event_type)

    await deps.event_bus.subscribe("*", _collect)

    final_state = await run_engagement(engagement, deps=deps)

    print("Events observed:", events)
    print("Final phase:", final_state.phase.value)
    print("Last verdict:", final_state.last_verdict)

    evidence = await deps.evidence_store.list_for_run(final_state.run_id)
    print(f"Evidence records for run: {len(evidence)}")


if __name__ == "__main__":
    asyncio.run(main())
