"""Structural shell-program discovery port (TASK-004).

The single source for shell programs is owned by TASK-003: the image
manifest file (generated from packages.txt as program-manifest.json,
crossed with dpkg-query so rows name real executables) for
image-provided programs, and the live workroom for instance-installed
programs. This port declares only the narrow async shape both sides
share. The capability-side provider implements it without importing
orchestration. Program rows remain structural; the optional execution metadata
seam uses the validated shared models below and performs no execution.

Callers never build the workroom key: they pass the trusted execution
identity (run_id plus required instance_id) and the provider derives the
key with its own workroom_key_for. The optional caller workroom_key is
compare-only (a mismatch raises) and discovery never passes one. A row
names an enumerated executable (absolute path) with its provenance;
package names alone never become cards.

Owner scopes come from the provider verbatim: "image" for image-provided
programs, "instance" for instance installs. An instance install is
information only and is never presented as a shared-catalog program.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ProgramExecutionRoute(BaseModel):
    """Provider declaration, not live reachability or execution permission."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    purpose: Literal["local", "target", "install"]
    status: Literal["local_only", "target_ready", "install_ready",
                    "configured_unverified", "unavailable"]
    reason_code: str
    tool_ids: list[str] = Field(default_factory=list)
    selectors: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)


class ProgramExecutionEnvironment(BaseModel):
    """Optional metadata seam for program discovery providers (version 1).

    A listed executable may require unsupported privileges. No per-program
    mode inference or target contact is performed by this metadata lookup.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    source_id: str
    routes: list[ProgramExecutionRoute]
    unsupported_modes: list[str] = Field(default_factory=list)
    target_execution_verified: Literal[False] = False
    execution_permission_granted: Literal[False] = False


class ProgramExecutionDiscovery(Protocol):
    def describe_execution_environment(self) -> ProgramExecutionEnvironment: ...


class ProgramDiscoveryError(Exception):
    """Owner-stable program discovery failure with a machine-readable code.

    Providers raise this (or a compatible error carrying reason_code) so
    callers surface the real reason instead of an exception type name.
    Agreed absent code: shell_workroom_not_running means the workroom was
    never created or is stopped, which callers report as a normal absent
    status rather than an error.
    """

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(reason_code + (": " + detail if detail else ""))
        self.reason_code = reason_code
        self.detail = detail


#: Provider codes that mean "nothing to list", reported as a normal absent
#: status rather than an error. Agreed with the TASK-003 provider, which
#: raises shell_workroom_not_running for a never-created or stopped room.
PROGRAM_ABSENT_CODES = frozenset({"shell_workroom_not_running"})


_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def program_error_code(exc: BaseException) -> str:
    """Stable code for a provider failure: validated reason_code, else type.

    Only a structural reason_code matching the stable code vocabulary is
    consumed; arbitrary exception text (paths, tokens, messages) never
    enters model-visible status. Anything else falls back to the exception
    type name.
    """

    code = getattr(exc, "reason_code", None)
    if isinstance(code, str) and _REASON_CODE_PATTERN.fullmatch(code):
        return code
    return type(exc).__name__


class ProgramRecord(Protocol):
    """One enumerated executable with provenance and version.

    Structural mirror of the TASK-003 ProgramRecord (frozen dataclass in
    redteam.capability.shell_workroom, re-exported from shell_assembly),
    consumed without importing it: name is the command as invoked (for
    example "dig", never the package name), executable is the absolute
    path, provenance is the apt origin or the instance install path.

    Members are read-only on purpose: the provider rows are frozen, and
    the common read contract must never imply mutation.
    """

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...

    @property
    def version(self) -> str | None: ...

    @property
    def owner_scope(self) -> str: ...

    @property
    def provenance(self) -> str | None: ...


class ShellProgramDiscovery(Protocol):
    """Narrow async seam to the TASK-003 program provider.

    source_id is the provider-owned source label (its own feature manifest
    serverId): card serverIds come from here, never from a central
    constant, so no common code names an individual provider.
    """

    @property
    def source_id(self) -> str: ...

    async def list_image_programs(
        self, manifest_path: str | None = None
    ) -> Sequence[ProgramRecord]: ...

    async def list_instance_programs(
        self,
        *,
        run_id: str,
        instance_id: str,
        workroom_key: str | None = None,
    ) -> Sequence[ProgramRecord]: ...
