"""``redteam roe show`` — human-readable projection of an Engagement's ROE.

Read-only: loads the config, prints it. No naming rule is invented here —
capability/operation vocabulary is `redteam.orchestration.action_naming`'s
(`docs/contracts/actions.md` "Action Naming Convention").
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import EXIT_OK, print_json
from redteam.core.config import load_engagement_config


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("roe", help="Inspect Rules of Engagement")
    roe_sub = parser.add_subparsers(dest="roe_command", required=True)

    show = roe_sub.add_parser("show", help="Show the effective ROE for an Engagement")
    show.add_argument("engagement_path")
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    config = load_engagement_config(args.engagement_path)

    if args.json:
        print_json(
            {
                "scope": config.scope.model_dump(),
                "rules_of_engagement": [rule.model_dump() for rule in config.rules_of_engagement],
                "approval_policy": config.approval_policy.model_dump(),
            }
        )
        return EXIT_OK

    scope = config.scope
    print("Scope")
    _print_list("  Allowed hosts", scope.allowed_hosts)
    _print_list("  Allowed domains", scope.allowed_domains)
    _print_list("  Allowed networks", scope.allowed_networks)
    _print_list("  Allowed assets", scope.allowed_assets)
    _print_list("  Denied hosts", scope.denied_hosts)
    _print_list("  Denied domains", scope.denied_domains)
    _print_list("  Denied assets", scope.denied_assets)

    if not config.rules_of_engagement:
        print("\nRules of Engagement: (none configured -- ROE stage is a no-op)")
    for rule in config.rules_of_engagement:
        print(f"\nRule: {rule.name}")
        if rule.description:
            print(f"  {rule.description}")
        _print_list("  Allowed capabilities", rule.allowed_capabilities)
        _print_list("  Denied capabilities", rule.denied_capabilities)
        _print_list("  Allowed operations", rule.allowed_operations)
        _print_list("  Denied operations", rule.denied_operations)
        if rule.start_at or rule.end_at:
            print(f"  Window: {rule.start_at or '(open)'} -> {rule.end_at or '(open)'}")
        _print_list("  Allowed time windows", rule.allowed_time_windows)
        print(f"  Availability impact allowed: {rule.availability_impact_allowed}")
        if rule.max_parallel_actions is not None:
            print(f"  Max parallel actions: {rule.max_parallel_actions}")
        if rule.prohibited_techniques:
            print(
                f"  Prohibited techniques: {', '.join(rule.prohibited_techniques)} "
                f"(WARNING: unenforceable -- `run` aborts closed on this)"
            )

    print(f"\nApproval policy: require_approval_for={config.approval_policy.require_approval_for}")
    return EXIT_OK


def _print_list(label: str, values: list[str]) -> None:
    if not values:
        return
    print(f"{label}:")
    for value in values:
        print(f"  - {value}")
