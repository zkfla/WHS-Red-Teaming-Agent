"""``python -m redteam.app.cli run examples/engagement.yaml``
``python -m redteam.app.cli approvals list``
``python -m redteam.app.cli approvals resolve <approval_id> approved --by <name>``

``run`` prints every published event, then the final ``RuntimeState``.

``approvals`` operates directly on the persisted ``ApprovalStore``
(default: ``data/approvals``) -- it does not need a ``run`` to be in
progress. Resolving here updates the Approval record on disk, but does
not (yet) produce an APPROVAL_RESOLVED audit entry when the approval
was requested by a *different* process than the one resolving it --
see the TODO in ``redteam.control.approval`` for why.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from redteam.app.bootstrap import build_runtime_deps, load_engagement, run_engagement
from redteam.control.approval import ApprovalService
from redteam.control.stores.filesystem import FilesystemApprovalStore
from redteam.core.contracts.events import EventEnvelope
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ApprovalId


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


async def _approvals_list(approval_dir: str) -> None:
    service = ApprovalService(FilesystemApprovalStore(approval_dir))
    pending = await service.list_pending()
    if not pending:
        print("No pending approvals.")
        return
    for approval in pending:
        print(
            f"{approval.approval_id}  action={approval.action_id}  "
            f"requested_at={approval.requested_at.isoformat()}"
        )


async def _approvals_resolve(
    approval_dir: str, approval_id: str, status: str, resolved_by: str
) -> None:
    service = ApprovalService(FilesystemApprovalStore(approval_dir))
    updated = await service.resolve(
        ApprovalId(approval_id), ApprovalStatus(status), resolved_by
    )
    print(f"{updated.approval_id} -> {updated.status.value} (by {updated.resolved_by})")


def main() -> None:
    logging.basicConfig(level=os.environ.get("REDTEAM_LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(prog="redteam")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run an engagement end-to-end (minimal vertical slice)"
    )
    run_parser.add_argument("engagement_path")

    approvals_parser = subparsers.add_parser(
        "approvals", help="Inspect/resolve pending Approvals (human-in-the-loop)"
    )
    approvals_sub = approvals_parser.add_subparsers(dest="approvals_command", required=True)

    list_parser = approvals_sub.add_parser("list", help="List pending approvals")
    list_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")

    resolve_parser = approvals_sub.add_parser("resolve", help="Approve or reject one")
    resolve_parser.add_argument("approval_id")
    resolve_parser.add_argument("status", choices=["approved", "rejected"])
    resolve_parser.add_argument("--by", required=True, dest="resolved_by")
    resolve_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")

    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.engagement_path))
    elif args.command == "approvals":
        if args.approvals_command == "list":
            asyncio.run(_approvals_list(args.approval_dir))
        elif args.approvals_command == "resolve":
            asyncio.run(
                _approvals_resolve(
                    args.approval_dir, args.approval_id, args.status, args.resolved_by
                )
            )


if __name__ == "__main__":
    main()
