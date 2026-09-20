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
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from redteam.core.config import AppConfig, load_engagement_config

# Exit codes (section 20): 0 success, 1 general run failure, 2 config/argument error.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2

# Single place default config paths are named (section 8's "Config Path
# Resolver") -- plain files in the current directory, the same
# convention `redteam init`'s `--output engagement.yaml` default and
# every existing example/doc already use. No new `.redteam/` directory:
# section 8 says prefer the existing convention when one already exists,
# and this repo's is "flat files in cwd".
DEFAULT_ENGAGEMENT_PATH = Path("engagement.yaml")
DEFAULT_APP_CONFIG_PATH = Path("app.yaml")
DEFAULT_ENV_PATH = Path(".env")

_SECRET_KEY_PATTERN = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|credential|auth|bearer|cookie)",
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


def load_app_config_for_run(
    engagement_path: str, config_path: str | None
) -> AppConfig:
    """The one place ``engagement.yaml`` (+ optional ``--config app.yaml``)
    becomes an ``AppConfig`` — every command that needs one goes through
    this, never a bespoke loader.

    The positional engagement file is always authoritative for
    ``engagement:``; an app-level ``--config`` file only supplies
    ``llm``/``orchestration``/``capability``/``logging`` (any
    ``engagement:`` key inside it is ignored — the CLI argument wins, so
    there is exactly one place a user looks for scope/ROE).
    """
    engagement_config = load_engagement_config(engagement_path)
    if config_path is None:
        return AppConfig(engagement=engagement_config)
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: application config must be a YAML mapping")
    raw = {**raw, "engagement": engagement_config.model_dump()}
    return AppConfig.model_validate(raw)


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


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))
    ]
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
