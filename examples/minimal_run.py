"""Runs the Minimal Vertical Slice programmatically (no CLI).

    python examples/minimal_run.py

Equivalent to ``python -m redteam.app.cli run examples/engagement.yaml``
but useful as a copy-pasteable starting point for team demos/tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from redteam.app.bootstrap import build_runtime_deps, load_engagement, run_engagement
from redteam.core.contracts.events import EventEnvelope

ENGAGEMENT_PATH = Path(__file__).parent / "engagement.yaml"


async def main() -> None:
    engagement = load_engagement(ENGAGEMENT_PATH)
    deps = await build_runtime_deps(engagement, evidence_dir="data/evidence")

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
