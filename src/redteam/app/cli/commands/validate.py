"""``redteam validate`` — check configuration without running anything.

Never touches a target: no MCP `discover_tools()`/`invoke()`, no
Control Plane `evaluate()` against a real action, no LLM call. Only
schema loading and the same canonical-naming check
(`orchestration.action_naming.canonical_action_key`) ROE itself uses —
so "valid" here means "the ROE stage will not immediately reject every
action for a naming mistake," not "this engagement is safe."
"""

from __future__ import annotations

import argparse

from pydantic import ValidationError

from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    print_json,
)
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.config import AppConfig, load_engagement_config
from redteam.orchestration.action_naming import canonical_action_key


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("validate", help="Validate an Engagement/App config")
    parser.add_argument("engagement_path")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, list[str]]] = []

    try:
        engagement_config = load_engagement_config(args.engagement_path)
    except (OSError, ValidationError) as exc:
        return _fail(args, [("Engagement", False, [str(exc)])])
    checks.append(("Engagement", True, []))

    try:
        # This command never calls an LLM (see module docstring) -- an
        # unset REDTEAM_LLM_PROVIDER must not block a pure config check.
        app_config = load_app_config_for_run(args.engagement_path, require_llm=False)
    except (OSError, ValidationError, ValueError) as exc:
        return _fail(args, [*checks, ("Application configuration", False, [str(exc)])])
    checks.append(("Application configuration", True, []))

    scope_problems = check_scope(engagement_config)
    checks.append(("Scope", not scope_problems, scope_problems))

    roe_problems = check_roe(engagement_config)
    checks.append(("ROE", not roe_problems, roe_problems))

    capability_problems = check_capability(app_config)
    checks.append(("Capability configuration", not capability_problems, capability_problems))

    ok = all(passed for _, passed, _ in checks)
    if args.json:
        print_json(
            {
                "valid": ok,
                "checks": [
                    {"name": name, "ok": passed, "problems": problems}
                    for name, passed, problems in checks
                ],
            }
        )
        return EXIT_OK if ok else EXIT_CONFIG_ERROR

    if ok:
        for name, _, _ in checks:
            # stdout may be a cp949 pipe/console on Windows. A Unicode
            # checkmark can make a valid legacy YAML exit with code 2.
            print(f"[OK] {name}")
        print("\nConfiguration valid.")
        return EXIT_OK
    return _fail(args, checks)


def _fail(args: argparse.Namespace, checks: list[tuple[str, bool, list[str]]]) -> int:
    if args.json:
        print_json(
            {
                "valid": False,
                "checks": [
                    {"name": name, "ok": passed, "problems": problems}
                    for name, passed, problems in checks
                ],
            }
        )
        return EXIT_CONFIG_ERROR
    eprint("Configuration invalid.\n")
    for name, passed, problems in checks:
        if passed:
            continue
        eprint(f"{name}:")
        for problem in problems:
            eprint(f"  {problem}")
    return EXIT_CONFIG_ERROR


def check_scope(engagement_config) -> list[str]:  # noqa: ANN001
    scope = engagement_config.scope
    problems: list[str] = []
    has_allowlist = (
        scope.allowed_hosts
        or scope.allowed_domains
        or scope.allowed_networks
        or scope.allowed_assets
    )
    if not has_allowlist:
        problems.append(
            "no allowed_hosts/allowed_domains/allowed_networks/allowed_assets configured "
            "-- every action will be denied by default-deny scope"
        )
    return problems


def check_roe(engagement_config) -> list[str]:  # noqa: ANN001
    problems: list[str] = []
    for rule in engagement_config.rules_of_engagement:
        for key_list, field_name in (
            (rule.denied_operations, "denied_operations"),
            (rule.allowed_operations, "allowed_operations"),
        ):
            for key in key_list:
                capability, _, operation = key.partition(".")
                try:
                    canonical_action_key(capability, operation)
                except ValueError as exc:
                    problems.append(
                        f"ROE rule '{rule.name}' {field_name}: unknown canonical operation "
                        f"{key!r} ({exc})"
                    )
        for cap_list, field_name in (
            (rule.denied_capabilities, "denied_capabilities"),
            (rule.allowed_capabilities, "allowed_capabilities"),
        ):
            for capability in cap_list:
                if capability not in {"recon", "web", "mobile", "analysis"}:
                    problems.append(
                        f"ROE rule '{rule.name}' {field_name}: unknown capability {capability!r}"
                    )
        if rule.prohibited_techniques:
            problems.append(
                f"ROE rule '{rule.name}' sets prohibited_techniques="
                f"{rule.prohibited_techniques!r}, which has no enforcement path yet "
                f"(redteam.control.roe.UnsupportedRoePolicyError) -- `run` will abort "
                f"the whole run the first time any action is evaluated"
            )
    return problems


def check_capability(app_config: AppConfig) -> list[str]:
    problems: list[str] = []
    if len(app_config.capability.mcp_servers) != len(set(app_config.capability.mcp_servers)):
        problems.append("capability.mcp_servers contains duplicate server ids")
    for server_id in app_config.capability.mcp_servers:
        try:
            MCPServerSettings.from_environment(server_id)
        except MCPConfigurationError as exc:
            problems.append(f"MCP server '{server_id}': {exc}")
    return problems
