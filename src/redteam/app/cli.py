"""``python -m redteam.app.cli run examples/engagement.yaml``

Prints every published event, then the final ``RuntimeState``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from redteam.app.bootstrap import build_runtime_deps, load_engagement, run_engagement
from redteam.core.contracts.events import EventEnvelope


async def _print_event(event: EventEnvelope) -> None:
    print(f"[{event.event_type}] {event.source} :: {event.payload}")


async def _run(engagement_path: str) -> None:
    engagement = load_engagement(engagement_path)
    deps = await build_runtime_deps(engagement)
    await deps.event_bus.subscribe("*", _print_event)

    final_state = await run_engagement(engagement, deps=deps)
    print(
        f"\nRun finished: phase={final_state.phase.value} "
        f"step_count={final_state.step_count} last_verdict={final_state.last_verdict}"
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("REDTEAM_LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(prog="redteam")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run an engagement end-to-end (minimal vertical slice)"
    )
    run_parser.add_argument("engagement_path")

    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.engagement_path))


if __name__ == "__main__":
    main()
