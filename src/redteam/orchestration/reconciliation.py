"""Minimal reconciliation host loop.

``AgentLoopRunner.reconcile_once()`` already does the real work for one
tick — event-coalesced wake-up with a periodic-reconciliation fallback
(``orchestration.wakeup.EventWakeup``), and (since this task) a dedicated
Approval-resume path. What was still missing was *something that calls it
repeatedly* for a run sitting in a reconcilable terminal state (including
``APPROVAL_PENDING``) so it actually gets revisited once conditions
change, instead of sitting terminated forever.

This is that minimal loop — a plain ``asyncio`` coroutine meant to be
awaited as a background task by whatever process embeds this library
(e.g. run it as one ``asyncio.create_task`` per active run, or drive it
from a bespoke worker). Deliberately **not**:

- a daemon/process-supervision framework (Celery, etc.) — the project's
  scale does not need one, and the caller already owns process lifetime;
- a distributed scheduler — one host loop tracks one run;
- a CLI entrypoint — out of scope for this change; the CLI is a separate,
  later integration (see ``redteam.app.cli``, untouched here).
"""

from __future__ import annotations

import asyncio

from redteam.orchestration.runner import RECONCILABLE_REASONS, AgentLoopRunner
from redteam.orchestration.state import RuntimeState


async def run_reconciliation_loop(
    runner: AgentLoopRunner,
    runtime: RuntimeState,
    *,
    poll_interval_seconds: float = 30.0,
    stop: asyncio.Event | None = None,
) -> RuntimeState:
    """Call ``runner.reconcile_once()`` until the run leaves every
    reconcilable reason (``RECONCILABLE_REASONS``), or ``stop`` fires.

    Each tick waits up to ``poll_interval_seconds`` for a relevant event
    before falling back to a plain reconciliation check (the same timeout
    ``reconcile_once``/``EventWakeup.wait`` already use) — this does not
    busy-poll. Returns the final ``RuntimeState``: either a genuinely
    terminal one (budget/step/failure limits, normal convergence, or a
    still-``APPROVAL_PENDING`` run if ``stop`` fired first) or, if the run
    keeps making progress, whatever state it reaches next time this
    function is called again with the returned runtime.
    """
    current = runtime
    while current.termination_reason in RECONCILABLE_REASONS:
        if stop is not None and stop.is_set():
            break
        result = await runner.reconcile_once(current, timeout_seconds=poll_interval_seconds)
        current = result.runtime
    return current
