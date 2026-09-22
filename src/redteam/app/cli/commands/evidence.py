"""``redteam evidence verify|purge`` — pre-existing commands, kept as-is.

Not part of this task's v1 command list, but already stable and not
asked to be removed (section 3: "현재 코드에 이미 안정적으로 구현되어
있다면 제거할 필요는 없습니다").
"""

from __future__ import annotations

import argparse
import math

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, eprint
from redteam.core.ids import RunId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "evidence", help="Verify integrity / apply retention on the persisted Evidence Store"
    )
    sub = parser.add_subparsers(dest="evidence_command", required=True)

    verify = sub.add_parser("verify", help="Check a run's evidence for tampering")
    verify.add_argument("--run", required=True, dest="run_id")
    verify.add_argument("--dir", default="data/evidence", dest="evidence_dir")
    verify.set_defaults(handler=run_verify)

    purge = sub.add_parser("purge", help="Delete runs past the retention window")
    purge.add_argument("--days", required=True, type=float)
    purge.add_argument("--dir", default="data/evidence", dest="evidence_dir")
    purge.set_defaults(handler=run_purge)


async def run_verify(args: argparse.Namespace) -> int:
    store = FilesystemEvidenceStore(args.evidence_dir)
    tampered = await store.verify_run_integrity(RunId(args.run_id))
    if not tampered:
        print(f"run {args.run_id}: OK, no tampering detected")
        return EXIT_OK
    print(f"run {args.run_id}: {len(tampered)} record(s) changed, missing, or unindexed:")
    for evidence_id in tampered:
        print(f"  {evidence_id}")
    return EXIT_FAILURE


async def run_purge(args: argparse.Namespace) -> int:
    if not math.isfinite(args.days) or args.days < 0:
        eprint("ERROR: --days must be finite and non-negative")
        return EXIT_CONFIG_ERROR
    store = FilesystemEvidenceStore(args.evidence_dir)
    purged = await store.purge_older_than(args.days)
    if not purged:
        print("Nothing to purge.")
        return EXIT_OK
    print(f"Purged {len(purged)} run(s):")
    for run_id in purged:
        print(f"  {run_id}")
    return EXIT_OK
