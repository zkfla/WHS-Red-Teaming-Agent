"""``redteam approvals list|approve|deny|resolve|expire``.

``approve``/``deny`` resolve through the durable Audit trail the same
way ``resolve`` always has (``AuditLog.find_approval_request`` /
``record_approval_resolution_from_request``) — not through
``DefaultControlPlane.resolve_approval``'s in-memory
``_pending_proposals``, which will not be populated in a fresh CLI
process (the ``run`` that produced this Approval already exited). This
is the one recovery path that actually works across that process
boundary; see ``redteam.control.service.DefaultControlPlane.approval_outcome``
for the "stale approval" note this sidesteps entirely by not depending
on in-memory state.

Resuming the Run itself is `run`'s job, not this command's: a still-live
``redteam run`` process (started without ``--non-interactive``) polls
for exactly this resolution and resumes on its own — see
``commands.run``. This command only ever marks the Approval durably; it
never executes an Action.
"""

from __future__ import annotations

import argparse
import math
import os

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, eprint, print_json
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ApprovalId


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("approvals", help="Inspect/resolve pending Approvals")
    sub = parser.add_subparsers(dest="approvals_command", required=True)

    list_parser = sub.add_parser("list", help="List pending approvals")
    list_parser.add_argument("--run", dest="run_id", default=None)
    list_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    # Only actually read when --run is given (run_list's own audit lookup is
    # conditional on args.run_id) -- added because run_list already reads
    # args.audit_dir unconditionally on that branch, which previously raised
    # AttributeError for any real `--run` invocation (untested: the existing
    # unit test only covers run_id=None).
    list_parser.add_argument("--audit-dir", default="data/audit", dest="audit_dir")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=run_list)

    approve_parser = sub.add_parser("approve", help="Approve a pending Action")
    approve_parser.add_argument("approval_id")
    approve_parser.add_argument("--by", dest="resolved_by", default=None)
    approve_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    approve_parser.add_argument("--audit-dir", default="data/audit")
    approve_parser.set_defaults(handler=run_approve)

    deny_parser = sub.add_parser("deny", help="Deny a pending Action")
    deny_parser.add_argument("approval_id")
    deny_parser.add_argument("--by", dest="resolved_by", default=None)
    deny_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    deny_parser.add_argument("--audit-dir", default="data/audit")
    deny_parser.set_defaults(handler=run_deny)

    resolve_parser = sub.add_parser("resolve", help="Approve or reject one (status as an argument)")
    resolve_parser.add_argument("approval_id")
    resolve_parser.add_argument("status", choices=["approved", "rejected"])
    resolve_parser.add_argument("--by", required=True, dest="resolved_by")
    resolve_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    resolve_parser.add_argument("--audit-dir", default="data/audit")
    resolve_parser.set_defaults(handler=run_resolve)

    expire_parser = sub.add_parser("expire", help="Expire pending approvals past a TTL")
    expire_parser.add_argument("--ttl-seconds", required=True, type=float)
    expire_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    expire_parser.set_defaults(handler=run_expire)
    for command in (list_parser, approve_parser, deny_parser, resolve_parser, expire_parser):
        command.add_argument("--state-db", default=None)
        if command is not list_parser:
            command.add_argument("--run", dest="run_id", default=None)


async def _central_approvals(args):
    """Resolve original Control stores and bind approval IDs to this central Run."""
    from redteam.app.cli._shared import central_reader_state_path
    path = (central_reader_state_path(args) if getattr(args, "run_id", None)
            or getattr(args, "state_db", None) is not None else None)
    if path is None:
        return None
    if not getattr(args, "run_id", None):
        raise ValueError("central_approvals_require_original_run_id")
    import sqlite3

    from redteam.app.central_views import inspect_central_run
    from redteam.orchestration.coordination import CoordinationError, StateConflict
    try:
        state, config, _ = await inspect_central_run(path, args.run_id)
    except (CoordinationError, StateConflict, sqlite3.Error):
        raise ValueError("central_original_approval_run_unavailable") from None
    args.approval_dir = config["storage_refs"]["approvals"]
    args.audit_dir = config["storage_refs"]["audit"]
    executions = await state.list_executions()
    expected = {str(item.approval_ref): str(item.action_id) for item in executions
                if item.approval_ref}
    identity = getattr(args, "approval_id", None)
    if identity is not None and identity not in expected:
        raise ValueError("approval_not_bound_to_original_central_execution")
    return expected


def _default_resolved_by() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "cli-operator"


async def run_list(args: argparse.Namespace) -> int:
    try:
        expected = await _central_approvals(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    service = ApprovalService(FilesystemApprovalStore(args.approval_dir, create=False))
    audit = AuditLog(FilesystemAuditStore(args.audit_dir, create=False)) if args.run_id else None
    pending = await service.list_pending()
    if expected is not None:
        pending = [a for a in pending if expected.get(str(a.approval_id)) == str(a.action_id)]
    if args.run_id and audit is not None:
        pending = [a for a in pending if await _belongs_to_run(audit, a, args.run_id)]

    if args.json:
        print_json([a.model_dump(mode="json") for a in pending])
        return EXIT_OK
    if not pending:
        print("No pending approvals.")
        return EXIT_OK
    for approval in pending:
        print(
            f"{approval.approval_id}  action={approval.action_id}  "
            f"requested_at={approval.requested_at.isoformat()}"
        )
    return EXIT_OK


async def _belongs_to_run(audit: AuditLog, approval, run_id: str) -> bool:  # noqa: ANN001
    request = await audit.find_approval_request(approval.action_id, approval.approval_id)
    return request is not None and str(request.run_id) == run_id


async def _show_and_resolve(args: argparse.Namespace, status: ApprovalStatus) -> int:
    try:
        expected = await _central_approvals(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    approvals = ApprovalService(FilesystemApprovalStore(args.approval_dir))
    audit = AuditLog(FilesystemAuditStore(args.audit_dir))

    approval = await approvals.get(ApprovalId(args.approval_id))
    if approval is None:
        eprint(f"ERROR: no such approval: {args.approval_id}")
        return EXIT_CONFIG_ERROR
    if approval.status != ApprovalStatus.PENDING:
        eprint(f"ERROR: approval {args.approval_id} is already {approval.status.value}.")
        return EXIT_CONFIG_ERROR

    request = await audit.find_approval_request(approval.action_id, approval.approval_id)
    if expected is not None and (expected.get(str(approval.approval_id)) != str(approval.action_id)
                                or request is None or str(request.run_id) != args.run_id):
        eprint("ERROR: approval_original_run_action_mismatched")
        return EXIT_FAILURE
    if request is None:
        eprint(
            f"ERROR: no policy-decision audit entry for approval {args.approval_id} -- "
            f"cannot recover which Run/Action this belongs to."
        )
        return EXIT_FAILURE

    summary = request.action_summary or {}
    print(f"Approval: {approval.approval_id}")
    print(f"Run: {request.run_id}")
    action_key = f"{summary.get('capability', '?')}.{summary.get('operation', '?')}"
    print(f"Action: {action_key}")
    target = summary.get("target")
    if isinstance(target, dict):
        print(f"Target: {target}")
    if summary.get("reason"):
        print(f"Reason: {summary['reason']}")
    if request.decision is not None and request.decision.reasons:
        print(f"Why approval was required: {'; '.join(request.decision.reasons)}")
    print("Risk: not available before Tool Selection (post-ALLOW) -- not shown")

    resolved_by = args.resolved_by or _default_resolved_by()
    try:
        updated = await approvals.resolve(ApprovalId(args.approval_id), status, resolved_by)
    except ValueError as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    await audit.record_approval_resolution_from_request(request, updated)
    print(f"\n{updated.approval_id} -> {updated.status.value} (by {updated.resolved_by})")
    if status == ApprovalStatus.APPROVED:
        print(
            "If the originating `redteam run` is still live and waiting "
            "(started without --non-interactive), it will notice this and "
            "resume the same Run on its own."
        )
    return EXIT_OK


async def run_approve(args: argparse.Namespace) -> int:
    return await _show_and_resolve(args, ApprovalStatus.APPROVED)


async def run_deny(args: argparse.Namespace) -> int:
    return await _show_and_resolve(args, ApprovalStatus.REJECTED)


async def run_resolve(args: argparse.Namespace) -> int:
    try:
        expected = await _central_approvals(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    approvals = ApprovalService(FilesystemApprovalStore(args.approval_dir))
    audit = AuditLog(FilesystemAuditStore(args.audit_dir))
    approval = await approvals.get(ApprovalId(args.approval_id))
    if approval is None:
        eprint(f"ERROR: no such approval: {args.approval_id}")
        return EXIT_CONFIG_ERROR
    request = await audit.find_approval_request(approval.action_id, approval.approval_id)
    if expected is not None and (expected.get(str(approval.approval_id)) != str(approval.action_id)
                                or request is None or str(request.run_id) != args.run_id):
        eprint("ERROR: approval_original_run_action_mismatched")
        return EXIT_FAILURE
    if request is None:
        eprint(f"ERROR: no policy decision audit entry for approval: {args.approval_id}")
        return EXIT_FAILURE
    try:
        updated = await approvals.resolve(
            ApprovalId(args.approval_id), ApprovalStatus(args.status), args.resolved_by
        )
    except ValueError as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    await audit.record_approval_resolution_from_request(request, updated)
    print(f"{updated.approval_id} -> {updated.status.value} (by {updated.resolved_by})")
    return EXIT_OK


async def run_expire(args: argparse.Namespace) -> int:
    if not math.isfinite(args.ttl_seconds) or args.ttl_seconds < 0:
        eprint("ERROR: --ttl-seconds must be finite and non-negative")
        return EXIT_CONFIG_ERROR
    try:
        expected = await _central_approvals(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    service = ApprovalService(FilesystemApprovalStore(args.approval_dir))
    expired = await service.sweep_expired(
        args.ttl_seconds, approval_ids=set(expected) if expected is not None else None,
    )
    print(f"Expired {len(expired)} approval(s).")
    for approval in expired:
        print(f"  {approval.approval_id}  action={approval.action_id}")
    return EXIT_OK
