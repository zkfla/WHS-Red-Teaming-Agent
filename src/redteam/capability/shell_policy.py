"""Operator-owned policy for stateful shell workrooms. Owned by Capability Team.

TASK-003: the shell workroom is a long-lived per-instance container entered
with ``docker exec``. The operator fixes this policy before the adapter
starts. Local execution defaults to ``--network none`` and this policy has
no networking switch. Egress requires a separate operator-owned network
policy and a validated gate namespace through the admission extension.

Script languages are fixed in code (bash, python3), not operator
configurable, so a policy file cannot widen the interpreter set.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Shared with the workroom manager (runtime check) and the tool manifest
# (descriptor JSON schema): one rule, defined once, consumed in both places.
WORKROOM_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_IDENTITY = re.compile(WORKROOM_KEY_PATTERN)
_IMAGE_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._/:@-]*@sha256:[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_SHELL_META = frozenset({";", "&", "|", "\x60", "$", ">", "<", "\r", "\n", "\x00"})


class ShellWorkroomPolicy(BaseModel):
    """Operator-owned Docker sandbox policy for shell workrooms."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    allow_unpinned_image: bool = False
    docker_binary: str = "docker"
    container_user: str = Field(default="65532:65532")
    workdir: str = Field(default="/workroom")
    memory: str = Field(default="512m")
    cpus: float = Field(default=1.0, gt=0, le=2.0)
    pids_limit: int = Field(default=128, ge=8, le=512)
    tmpfs_bytes: int = Field(default=67108864, ge=1048576, le=268435456)
    max_script_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_timeout_seconds: float = Field(default=300.0, gt=0, le=600.0)
    default_timeout_seconds: float = Field(default=60.0, gt=0, le=600.0)
    max_jobs: int = Field(default=32, ge=1, le=256)
    completed_job_ttl_seconds: float = Field(default=300.0, ge=1.0, le=86400.0)
    max_workrooms: int = Field(default=64, ge=1, le=512)
    exec_grace_seconds: float = Field(default=2.0, ge=1.0, le=30.0)

    @field_validator("docker_binary")
    @classmethod
    def single_binary_path(cls, value: str) -> str:
        if not value or any(c in value for c in _SHELL_META):
            raise ValueError("docker_binary must be one executable path")
        return value

    @field_validator("container_user")
    @classmethod
    def valid_user(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", value):
            raise ValueError("container_user must be uid:gid")
        return value

    @field_validator("workdir")
    @classmethod
    def absolute_workdir(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError("workdir must be an absolute path without ..")
        return value

    @field_validator("memory")
    @classmethod
    def valid_memory(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]{0,3}[mMgG]", value):
            raise ValueError("memory must match <int>[mMgG]")
        return value

    def validate_image(self) -> None:
        """Fail unless the image is content-pinned or explicitly unpinned.

        Two pinned forms: a registry digest reference
        (``repo@sha256:<digest>``) and a local immutable image ID
        (``sha256:<id>``) as reported by the Docker CLI. Both are
        content-addressed, so neither widens to tags.
        """
        if self.allow_unpinned_image:
            return
        if not _IMAGE_DIGEST.fullmatch(self.image) and not _IMAGE_ID.fullmatch(self.image):
            raise ValueError("shell workroom image must be pinned by sha256 digest or image ID")

    @classmethod
    def valid_workroom_key(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("workroom keys must be bounded opaque identifiers")
        return value


def load_shell_workroom_policy(path: str) -> ShellWorkroomPolicy:
    """Load an operator-owned shell-workroom policy from a JSON file.

    Owned by the Capability team. The file must hold a JSON object matching
    ShellWorkroomPolicy fields; the image pin is enforced here via
    validate_image, so an unpinned image without explicit opt-in fails
    before any adapter or container exists. All failures (missing file,
    bad JSON, schema violation, unpinned image) raise ValueError with the
    path in the message; the ShellRuntime builder maps them to
    MCPConfigurationError for the CLI config-error path.
    Schema violations carry only a bounded field/type diagnostic (never raw
    input values); all other messages carry the path plus the stable cause.
    """
    import json
    from pathlib import Path

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"shell workroom policy unreadable ({path}): {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"shell workroom policy is not valid JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"shell workroom policy must be a JSON object ({path})")
    try:
        policy = ShellWorkroomPolicy(**data)
    except ValidationError as exc:
        raise ValueError(
            f"shell workroom policy invalid ({path}): {validation_diagnostic(exc)}"
        ) from exc
    try:
        policy.validate_image()
    except ValueError as exc:
        raise ValueError(f"shell workroom policy image not pinned ({path}): {exc}") from exc
    return policy


_MAX_POLICY_FIELD_ISSUES = 3


def validation_diagnostic(exc: ValidationError) -> str:
    """Bounded field/type diagnostic with no input values.

    Shared by every operator-policy loader (shell workroom, gated network):
    only dotted field paths and stable error codes travel, capped at
    _MAX_POLICY_FIELD_ISSUES with a remainder count.

    Same rule as the diagnostics boundary (include_input=False is mandatory):
    str(ValidationError) embeds input_value, so raw operator file values
    (potentially secrets or malformed sensitive values) must never reach
    CLI/MCP error text. Only dotted field paths and stable error codes
    travel, capped at _MAX_POLICY_FIELD_ISSUES with a remainder count.
    """
    details = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False)[
        :_MAX_POLICY_FIELD_ISSUES
    ]:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        details.append(f"{loc}: {error.get('type', 'validation')}")
    total = exc.error_count()
    extra = f" (+{total - len(details)} more)" if total > len(details) else ""
    return f"{total} field error(s): " + ", ".join(details) + extra
