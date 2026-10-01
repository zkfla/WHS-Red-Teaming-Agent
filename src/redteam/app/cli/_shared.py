"""Shared CLI plumbing: config loading, secret masking, rendering, exit codes.

Everything here is glue — argument parsing, calling into
``redteam.app.bootstrap``/``redteam.control``/``redteam.capability``, and
formatting output. No ROE/Scope/policy/tool-selection decision is made in
this package; every command hands the real decision to the domain layer
and only renders what comes back.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from redteam.capability.settings import mcp_server_ids_from_environment
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    LLMConfig,
    MethodologyConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.redaction import SECRET_KEY_PARTS

# Exit codes (section 20): 0 success, 1 general run failure, 2 config/argument error.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2

# Single place default config paths are named (section 8's "Config Path
# Resolver") -- plain files in the current directory, the same
# convention `redteam init`'s `--output engagement.yaml` default and
# every existing example/doc already use. No new `.redteam/` directory:
# section 8 says prefer the existing convention when one already exists,
# and this repo's is "flat files in cwd". There is no application-config
# YAML file (``app.yaml``) in this CLI -- everything besides the
# engagement lives in the environment (``REDTEAM_LLM_*``/``REDTEAM_MCP_*``).
DEFAULT_ENGAGEMENT_PATH = Path("engagement.yaml")
DEFAULT_ENV_PATH = Path(".env")

# Built from the one shared vocabulary (core.redaction.SECRET_KEY_PARTS,
# also used by core.contracts.events/mission and
# orchestration.operator_intents.redaction) plus "auth" as an
# additional, deliberately broader CLI-only substring match (output
# masking is one-way and fail-safe here, so over-matching is cheap and
# under-matching a printed secret is not).
_SECRET_KEY_PATTERN = re.compile(
    "(" + "|".join(re.escape(part) for part in (*SECRET_KEY_PARTS, "auth")) + ")",
    re.IGNORECASE,
)


def eprint(*args: object, **kwargs: object) -> None:
    """Print to stderr — keeps stdout clean for ``--json`` machine output."""
    print(*args, file=sys.stderr, **kwargs)  # type: ignore[call-overload]


def mask_secrets(value: Any) -> Any:
    """Best-effort redaction before anything from config/env ever prints.

    Mirrors ``redteam.control.audit.redact_arguments`` — same rationale,
    same limitation (obviously-named keys and known patterns only, not a
    substitute for not collecting secrets in the first place).
    """
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if _SECRET_KEY_PATTERN.search(str(key)) else mask_secrets(v))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def load_app_config_for_run(engagement_path: str, *, require_llm: bool = True) -> AppConfig:
    """The one place ``engagement.yaml`` + environment becomes an
    ``AppConfig`` — every command that needs one goes through this, never
    a bespoke loader.

    The positional engagement file is always authoritative for
    ``engagement:``. Everything else -- ``llm``, ``capability.mcp_servers``,
    each MCP server's own connection details -- comes from the environment
    (``REDTEAM_LLM_*`` / ``REDTEAM_MCP_*``), never from a YAML application
    config file: there is no ``app.yaml``/``--config`` path in this CLI.

    ``require_llm`` gates whether an unset ``REDTEAM_LLM_PROVIDER`` is a
    hard error (``LLMConfig.from_environment()``) or the plain, unused
    ``LLMConfig()`` default. ``redteam run``/``redteam resume`` (they
    actually build an ``LLMClient`` and execute an Action) pass the
    default ``True``; ``redteam validate`` -- which, by its own module
    docstring, never calls an LLM at all -- passes ``False`` so a pure
    config-inspection command is never blocked on a decision it does not
    need.
    """
    engagement_config = load_engagement_config(engagement_path)
    from redteam.app.mcp_deployment import synchronize_deployment

    server_ids = mcp_server_ids_from_environment()
    engagement_config = synchronize_deployment(engagement_config, server_ids)
    return AppConfig(
        engagement=engagement_config,
        llm=LLMConfig.from_environment() if require_llm else LLMConfig(),
        orchestration=OrchestrationConfig.from_environment(),
        capability=CapabilityConfig(
            mcp_servers=server_ids,
            methodology=MethodologyConfig.from_environment(),
        ),
    )


def print_json(payload: BaseModel | dict[str, Any] | list[Any]) -> None:
    """The only thing written to stdout in ``--json`` mode — logs/warnings
    go through ``eprint``/the logging subsystem so stdout stays valid JSON.
    """
    data: Any
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    elif isinstance(payload, list):
        data = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in payload
        ]
    else:
        data = payload
    print(json.dumps(mask_secrets(data), indent=2, sort_keys=True))


def prompt_secret(label: str) -> str:
    """Read a secret from the terminal without echoing it (section 7).

    Never call this from anywhere that also talks to an LLM in the same
    flow — the Intake Assistant (``redteam.intake``) has no code path
    that could reach this function, by construction (it never handles
    ``EngagementDraft``-shaped data with a secret field at all).
    """
    return getpass.getpass(f"{label}: ")


def env_file_is_gitignored(env_path: Path, *, repo_root: Path | None = None) -> bool:
    """Best-effort check that ``env_path`` (typically ``.env``) is covered
    by a ``.gitignore`` in ``repo_root`` (default: ``env_path``'s parent).
    Not a substitute for a real secret store — just the warning section 7
    asks for when falling back to a plain ``.env`` file.
    """
    root = repo_root or env_path.parent
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return False
    name = env_path.name
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped in {name, f"/{name}", f"{name}/"}:
            return True
    return False


def write_env_var(env_path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in a ``.env``-style file, updating the line in
    place if ``key`` is already present, appending otherwise. Never logs
    or echoes ``value`` — callers control that, and none of them do.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_dotenv_if_present(env_path: Path = DEFAULT_ENV_PATH) -> None:
    """Best-effort ``.env`` loader for this process's own environment.

    A real shell/CI export always wins -- this only fills in what is
    otherwise unset, so it can never override an operator's explicit
    environment with a stale ``.env`` value. Not a config source of truth
    by itself: it exists purely so a value ``write_env_var`` (or an
    operator by hand) put in ``.env`` takes effect without a separate
    manual ``export``/`source`` step before every ``redteam`` invocation.
    A missing file, unreadable file, or malformed line is silently
    skipped -- never fatal, and never a reason to block the CLI.
    """
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
