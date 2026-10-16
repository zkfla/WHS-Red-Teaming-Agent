"""``redteam validate`` — check configuration without running anything.

Never touches a target: no MCP `discover_tools()`/`invoke()`, no
Control Plane `evaluate()` against a real action, no LLM call. Only
schema/policy/profile loading and the same canonical-naming check
(`orchestration.action_naming.canonical_action_key`) ROE itself uses —
so "valid" here means "the ROE stage will not immediately reject every
action for a naming mistake," not "this engagement is safe."
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from pathlib import Path

from pydantic import ValidationError

from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    print_json,
)
from redteam.app.mcp_deployment import DeploymentBinding, verified_deployment
from redteam.app.web_policy import deployment_environment
from redteam.capability.deployment import DeploymentSpec
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.core.config import AppConfig, EngagementConfig, LLMConfig, load_engagement_config
from redteam.core.environment import env_snapshot, environment_context
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
        # Read-only: a recorded deployment is checked under its composed MCP environment,
        # in memory only. Nothing is written. This command never calls an LLM (see module
        # docstring), so an unset REDTEAM_LLM_PROVIDER must not block a pure config check.
        startup_environment = _recorded_startup_environment(engagement_config)
        scope = (
            environment_context(startup_environment)
            if startup_environment is not None else nullcontext()
        )
        with scope:
            app_config = load_app_config_for_run(args.engagement_path, require_llm=False)
            capability_problems = check_capability(app_config)
    except (OSError, ValidationError, ValueError) as exc:
        return _fail(args, [*checks, ("Application configuration", False, [str(exc)])])
    checks.append(("Application configuration", True, []))
    # Parse configured LLM settings and check Go's model shape offline.
    # An unset provider never blocks the pure engagement checks.
    checks.append(check_llm(env_snapshot()))

    scope_problems = check_scope(engagement_config)
    checks.append(("Scope", not scope_problems, scope_problems))

    roe_problems = check_roe(engagement_config)
    checks.append(("ROE", not roe_problems, roe_problems))

    checks.append(("Capability configuration", not capability_problems, capability_problems))
    checks.extend(check_execution_configuration(app_config))

    ok = all(passed for _, passed, _ in checks)
    if args.json:
        print_json(
            {
                "valid": ok,
                "live_readiness_checked": False,
                "admission_checked": False,
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
        print("Offline checks only; live readiness and execution permission were not checked.")
        return EXIT_OK
    return _fail(args, checks)


_SAFE_DEPLOYMENT_FAILURE = (
    "MCP deployment validation failed; approved settings are missing or inconsistent"
)


def _recorded_startup_environment(engagement_config: EngagementConfig) -> dict[str, str] | None:
    """Compose a recorded deployment's MCP environment without writing anything.

    Full verification (digests, scope, roles) runs here; the environment-dependent
    checks run afterwards in load_app_config_for_run under the composed environment.
    Artifact parse failures are masked: parser messages can echo input or paths.
    """
    raw = engagement_config.metadata.get("mcp_deployment")
    if raw is None:
        return None
    try:
        binding = DeploymentBinding.model_validate(raw)
        declared = DeploymentSpec.model_validate_json(Path(binding.source_path).read_bytes())
    except (OSError, ValueError):  # pydantic ValidationError is a ValueError
        raise ValueError(_SAFE_DEPLOYMENT_FAILURE) from None
    verified = verified_deployment(
        engagement_config, list(declared.roles), check_environment=False,
    )
    if verified is None:
        return None
    return deployment_environment(verified.spec, verified.binding)


def _fail(args: argparse.Namespace, checks: list[tuple[str, bool, list[str]]]) -> int:
    if args.json:
        print_json(
            {
                "valid": False,
                "live_readiness_checked": False,
                "admission_checked": False,
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


def check_llm(environment) -> tuple[str, bool, list[str]]:  # noqa: ANN001
    """Offline LLM selection check: ordinary parsing plus a Go-only extra.

    Uses LLMConfig.from_environment for the ordinary path, so defaults
    (e.g. the litellm model) behave exactly as before. Only when the Go
    provider is selected, the shared feature-owned helper validates the
    model shape offline. Zero provider calls. An unset REDTEAM_LLM_PROVIDER
    is informational, never a blocker. A missing key *value* is a labelled
    pass, never an error: the value is only needed at run time. An
    unrecognized provider name stays a startup error at run time
    (build_llm_client), not an offline failure here. Cannot validate
    offline: key validity, endpoint reachability, model existence/permission,
    quota/429 behavior, or any live model quality -- no live paid testing.
    """
    from redteam.llm.opencode_go_config import is_go_provider, validate_model

    provider = (environment.get("REDTEAM_LLM_PROVIDER") or "").strip()
    if not provider:
        return ("LLM configuration (not configured)", True, [])
    try:
        llm_config = LLMConfig.from_environment(environment)
    except ValueError as exc:
        return ("LLM configuration", False, [str(exc)])
    if is_go_provider(llm_config.provider):
        try:
            validate_model(llm_config.default_model)
        except ValueError as exc:
            return ("LLM configuration", False, [str(exc)])
    if llm_config.default_model.startswith("chatgpt/"):
        # OAuth device flow: no API key value to check.
        return ("LLM configuration", True, [])
    if not environment.get(llm_config.api_key_env_var):
        return (
            "LLM configuration (API key value not set here -- required at run time)",
            True,
            [],
        )
    return ("LLM configuration", True, [])


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


def check_execution_configuration(app_config: AppConfig) -> list[tuple[str, bool, list[str]]]:
    """Use the same provider/profile factories as run, without opening them.

    Disabled selections are valid but explicitly labelled: they do not supply
    tools. No Docker inspect/pull, MCP connection, grant, store or model call.
    """
    from redteam.app.agent_profile import selected_specialists
    from redteam.app.bootstrap import build_selected_code_fixture_runner
    from redteam.capability.settings import (
        automatic_shell_setup_from_environment,
        gated_network_policy_path_from_environment,
        shell_image_profile_from_environment,
        shell_policy_path_from_environment,
    )
    from redteam.capability.shell_assembly import build_gated_provider_config
    from redteam.capability.shell_policy import load_shell_workroom_policy

    checks: list[tuple[str, bool, list[str]]] = []
    environment = env_snapshot()
    shell_path = shell_policy_path_from_environment(source=environment)
    gate_path = gated_network_policy_path_from_environment(source=environment)
    try:
        auto_setup = automatic_shell_setup_from_environment(source=environment)
    except MCPConfigurationError as exc:
        checks.append(("Automatic shell setup", False, [str(exc)]))
        auto_setup = False
    try:
        image_profile = shell_image_profile_from_environment(source=environment)
    except MCPConfigurationError as exc:
        checks.append(("Kali image profile", False, [str(exc)]))
        image_profile = "invalid"
    else:
        checks.append((f"Kali image profile ({image_profile})", True, []))
    if shell_path is not None and image_profile != "baseline":
        checks.append((
            "Kali image profile selection",
            False,
            ["managed profiles cannot be combined with REDTEAM_SHELL_POLICY_PATH"],
        ))
    if shell_path is None:
        label = (
            "Shell policy (automatic preparation on run)" if auto_setup
            else "Shell policy (disabled)"
        )
        checks.append((label, True, []))
    else:
        try:
            load_shell_workroom_policy(shell_path)
        except ValueError as exc:
            checks.append(("Shell policy", False, [str(exc)]))
        else:
            checks.append(("Shell policy", True, []))
    if gate_path is None:
        checks.append(("Gated network policy (closed)", True, []))
    else:
        problems = []
        if shell_path is None and not auto_setup:
            problems.append("gated network policy requires REDTEAM_SHELL_POLICY_PATH")
        try:
            build_gated_provider_config(gate_path)
        except MCPConfigurationError as exc:
            problems.append(str(exc))
        checks.append(("Gated network policy", not problems, problems))
    specialists = None
    try:
        specialists = selected_specialists(app_config.orchestration)
    except ValueError as exc:
        checks.append(("Specialist profile", False, [str(exc)]))
    else:
        name = "Specialist profile" if specialists is not None else "Specialist profile (disabled)"
        checks.append((name, True, []))
    try:
        fixture = build_selected_code_fixture_runner(specialists, None, app_config.orchestration)
    except ValueError as exc:
        checks.append(("Offline fixture policy", False, [str(exc)]))
    else:
        name = (
            "Offline fixture policy" if fixture is not None else "Offline fixture policy (disabled)"
        )
        checks.append((name, True, []))
    code_enabled = specialists is not None and specialists.code_artifacts_enabled
    checks.append((
        "Code artifacts (enabled)" if code_enabled else "Code artifacts (disabled)", True, [],
    ))
    return checks


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
        # The explicit in-process example adapter is a sentinel consumed by
        # app.bootstrap, not a configured external MCP server. Requiring
        # REDTEAM_MCP_EXAMPLE_* here made the interactive Preflight reject the
        # same configuration that `redteam run` correctly accepts.
        if server_id == "example":
            continue
        try:
            MCPServerSettings.from_environment(server_id)
        except MCPConfigurationError as exc:
            problems.append(f"MCP server '{server_id}': {exc}")
    return problems
