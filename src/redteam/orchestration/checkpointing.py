"""LangGraph checkpointing.

One snapshot per super-step, enabling ``state_history`` and replay.

The allowlist is load-bearing: LangGraph only reconstructs types on an
explicit ``(module, name)`` allowlist and silently returns raw dicts for
anything else, so it is derived by walking our own packages rather than
typed out by hand. Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from redteam.core.ids import EngagementId
    from redteam.orchestration.component_binding import RuntimeComponentBinding

#: Packages whose types may legitimately appear inside ``GraphState``.
STATE_PACKAGES = ("redteam.core", "redteam.attack_graph", "redteam.orchestration")


class StateCodecRegistry:
    """Per-run holder for selected ``state_codec`` contributions.

    One registry per checkpointer/run: it carries only the contributions
    the app selected for that execution, so a codec registered for run A
    never leaks into run B. Fresh registries per run; never a global.
    """

    def __init__(
        self,
        entries: Iterable[tuple[str, str] | tuple[str, str, str | None]] = (),
    ) -> None:
        self._entries: dict[tuple[str, str], str | None] = {}
        for entry in entries:
            if len(entry) == 2:
                module, name = entry
                version = None
            else:
                module, name, version = entry
            self.register(module, name, version=version)

    def register(self, module: str, name: str, *, version: str | None = None) -> None:
        """Add one ``(module, name)`` type; malformed input fails loudly."""
        if not module or not str(module).strip() or not name or not str(name).strip():
            raise ValueError("state codec needs a non-empty module and name")
        self._entries[(str(module).strip(), str(name).strip())] = version

    def entries(self) -> tuple[tuple[str, str], ...]:
        """Sorted ``(module, name)`` pairs selected for this run."""
        return tuple(sorted(self._entries))

    def versions(self) -> dict[tuple[str, str], str | None]:
        """Recorded content versions per selected type (metadata only)."""
        return dict(self._entries)


#: Legacy/default-path registry for explicitly registered out-of-package
#: state types. Maps ``(module, name)`` to the recorded version. This is
#: *only* consulted by the default path (no registry passed); the selection
#: path (registry passed) never merges it, so accumulation here cannot widen
#: another run's permission.
_EXTERNAL_STATE_TYPES: dict[tuple[str, str], str | None] = {}


#: Contribution kind consumed as checkpoint state codecs (see
#: ``redteam.core.extensions.Contribution``). The ``contribution_id`` carries
#: the target type as ``"{module}:{name}"``; ``version`` is recorded as-is.
#: No factory is invoked here: the checkpoint serializer consumes only the
#: ``(module, name)`` allowlist, so collection stays declaration-only.
STATE_CODEC_KIND = "state_codec"


def register_state_type(module: str, name: str, *, version: str | None = None) -> None:
    """Register one external state type on the legacy/default path.

    ``STATE_PACKAGES`` scanning never covers outside packages; call this
    for default-path runs whose ``GraphState`` may hold the type. This
    only feeds the default path (no registry passed to the checkpointer
    builders); the per-run selection path never merges it.

    There is no ``restore_provider`` argument: no serde consumes such an
    object (the serializer only takes an allowlist of ``(module, name)``
    pairs), so accepting one would name a port that has no consumer.
    """
    if not module or not str(module).strip() or not name or not str(name).strip():
        raise ValueError("register_state_type needs a non-empty module and name")
    _EXTERNAL_STATE_TYPES[(str(module).strip(), str(name).strip())] = version


def registered_state_types() -> dict[tuple[str, str], str | None]:
    """Return a copy of the legacy explicit-type registry."""
    return dict(_EXTERNAL_STATE_TYPES)


def _split_codec_id(contribution: Any) -> tuple[str, str, str | None]:
    """Split a ``state_codec`` contribution into ``(module, name, version)``.

    ``contribution`` is a ``redteam.core.extensions.Contribution`` whose
    ``contribution_id`` is ``"{module}:{name}"``. Wrong kind or malformed
    id fails loudly. The contribution factory is *not* called here:
    declaration collection stays free of execution.
    """
    kind = getattr(contribution, "kind", None)
    if kind != STATE_CODEC_KIND:
        raise TypeError(f"state codec contribution needs kind {STATE_CODEC_KIND!r}, got {kind!r}")
    contribution_id = getattr(contribution, "contribution_id", "")
    module, separator, name = str(contribution_id).partition(":")
    if not separator or not module.strip() or not name.strip() or ":" in name:
        raise ValueError(
            "state codec contribution_id must look like '{module}:{name}', "
            f"got {contribution_id!r}"
        )
    return module.strip(), name.strip(), getattr(contribution, "version", None)


def register_state_codec(
    contribution: Any, *, registry: StateCodecRegistry | None = None, legacy: bool = False
) -> None:
    """Consume one ``state_codec`` contribution into an explicit registry.

    The destination is explicit: pass the run's ``registry``, or pass
    ``legacy=True`` to land on the legacy/default path. With neither, this
    raises instead of quietly widening the global registry. ``registry``
    and ``legacy`` together are rejected as contradictory.
    """
    if registry is not None and legacy:
        raise ValueError("register_state_codec needs registry or legacy, not both")
    module, name, version = _split_codec_id(contribution)
    if registry is not None:
        registry.register(module, name, version=version)
    elif legacy:
        register_state_type(module, name, version=version)
    else:
        raise ValueError(
            "register_state_codec needs an explicit registry (or legacy=True for the default path)"
        )


def consume_state_codec_contributions(
    contributions: Any, *, registry: StateCodecRegistry | None = None, legacy: bool = False
) -> int:
    """Register every ``state_codec`` contribution; return how many joined.

    Destination rule is the same as :func:`register_state_codec`: an
    explicit ``registry``, or ``legacy=True``. The first malformed entry
    aborts loudly with nothing silently skipped. Feed this with
    ``ExtensionAssembly.contributions(STATE_CODEC_KIND)``; assembly wiring
    itself stays with the app.
    """
    count = 0
    for contribution in contributions:
        register_state_codec(contribution, registry=registry, legacy=legacy)
        count += 1
    return count


def ensure_state_types_allowed(value: Any, *, registry: StateCodecRegistry | None = None) -> None:
    """Reject model/Enum values outside this run's allowlist.

    Walks mappings, sequences, sets and Pydantic models. Any model/Enum
    instance whose ``(module, name)`` is absent from this run's allowlist
    raises ``TypeError`` naming the type, instead of letting the
    checkpoint serializer demote it to a raw dict. The allowlist is the
    same one the builders hand to the serializer: package scan plus the
    selected ``registry`` when one is passed, else scan plus the legacy
    global registry. A selection therefore never inherits globally
    registered types it did not select.
    """
    allowed = set(explicit_allowlist(registry) if registry is not None else legacy_allowlist())
    seen: set[int] = set()

    def visit(node: Any) -> None:
        # Enum first: StrEnum/IntEnum are also str/int instances, so the
        # primitive early-return below would otherwise skip the allowlist.
        if isinstance(node, Enum):
            module, name = type(node).__module__, type(node).__name__
            if (module, name) not in allowed:
                raise TypeError(f"unregistered checkpoint state type: {module}.{name}")
            return
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(node, BaseModel):
            module, name = type(node).__module__, type(node).__name__
            if (module, name) not in allowed:
                raise TypeError(f"unregistered checkpoint state type: {module}.{name}")
            for field_value in node.__dict__.values():
                visit(field_value)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                visit(key)
                visit(item)
            return
        if isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                visit(item)
            return

    visit(value)


def _fail_on_scan_error(package_name: str) -> None:
    """``pkgutil.walk_packages`` silently swallows an ``ImportError`` while
    scanning a package when no ``onerror`` is given -- meaning a broken
    submodule would silently *exclude* every state type it defines from
    the checkpoint allowlist, with no error at all (the exact "unknown
    type falls through silently" failure section 11 asks not to allow,
    just one step earlier: silently absent from the allowlist, rather
    than present but unrecognized). Re-raise instead of swallowing.
    """
    raise ImportError(f"checkpoint state scan could not import package {package_name!r}")


@lru_cache(maxsize=1)
def state_type_allowlist() -> tuple[tuple[str, str], ...]:
    """Every first-party Pydantic model / Enum that can appear in ``GraphState``.

    Deliberately still discovered by walking ``STATE_PACKAGES`` rather
    than hand-typed as an explicit tuple: the alternative is a
    hand-maintained list of every ``BaseModel``/``Enum`` across three
    large packages (core, attack_graph, orchestration), which would
    silently go stale the next time someone adds a new state-carrying
    model and forgets to extend it -- a worse silent-failure mode than
    the one this reflection was meant to solve. What *is* made explicit,
    and was not before: a broken submodule (an ``ImportError`` while
    walking a package) now fails this call loudly instead of the walk
    silently pretending that submodule, and every state type it defines,
    does not exist. This is the scan only: no global or per-run registry
    is merged here.
    """
    found: set[tuple[str, str]] = set()
    for package_name in STATE_PACKAGES:
        package = importlib.import_module(package_name)
        modules = [package]
        for info in pkgutil.walk_packages(
            getattr(package, "__path__", []),
            prefix=f"{package_name}.",
            onerror=_fail_on_scan_error,
        ):
            modules.append(importlib.import_module(info.name))

        for module in modules:
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if not obj.__module__.startswith("redteam."):
                    continue
                if issubclass(obj, (BaseModel, Enum)):
                    found.add((obj.__module__, obj.__name__))
    return tuple(sorted(found))


def legacy_allowlist() -> tuple[tuple[str, str], ...]:
    """Scan plus the legacy global registry: the default path only."""
    return tuple(sorted(set(state_type_allowlist()) | set(_EXTERNAL_STATE_TYPES)))


def explicit_allowlist(registry: StateCodecRegistry | None = None) -> tuple[tuple[str, str], ...]:
    """Scan plus one run's selected registry; never the legacy global."""
    base = set(state_type_allowlist())
    if registry is not None:
        base |= set(registry.entries())
    return tuple(sorted(base))


#: Msgpack restore events meaning the installed SDK refused a type and fell
#: back to raw data (the False paths of its own allowlist and method gates).
#: With an explicit allowlist the SDK never emits the allow-with-warning
#: kind, so only these two blocked kinds are watched.
_BLOCKED_MSGPACK_KINDS = frozenset({"msgpack_blocked", "msgpack_method_blocked"})

#: Per-load collection box for the blocked events above. None outside a
#: scoped loads_typed call; each call sets its own list and resets the
#: token in finally, so nested or concurrent loads never share a box.
_active_blocked_events: ContextVar[list[Mapping[str, Any]] | None] = ContextVar(
    "checkpoint_blocked_events", default=None
)


def _describe_blocked_event(event: Mapping[str, Any]) -> str:
    """Render one blocked restore event as module.name[.method]."""
    module = event.get("module", "?")
    name = event.get("name", "?")
    method = event.get("method")
    if method:
        return f"{module}.{name}.{method}"
    return f"{module}.{name}"


def _serializer(
    allowlist: tuple[tuple[str, str], ...], registry: StateCodecRegistry | None = None
) -> Any:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    resolved = tuple(allowlist)
    run_registry = registry
    json_allowlist = [tuple(str(module).split(".") + [str(name)]) for module, name in resolved]

    class _RunScopedSerializer(JsonPlusSerializer):
        # Installed SDK serializer with this run allowlist on both edges.
        # dumps_typed checks first then delegates to super; loads_typed
        # lets the SDK restore run, then turns its own blocked-restore
        # events into a loud TypeError. No new format.

        def __init__(self) -> None:
            super().__init__(
                allowed_msgpack_modules=list(resolved),
                allowed_json_modules=json_allowlist,
            )
            self._state_registry = run_registry

        def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
            ensure_state_types_allowed(obj, registry=self._state_registry)
            return super().dumps_typed(obj)

        def _reviver(self, value: dict[str, Any]) -> Any:
            # Same envelope gate as the installed SDK _reviver: run the
            # SDK own allowlist check first so its refusal propagates
            # instead of being swallowed into a raw fallback.
            if (
                value.get("lc", None) == 2
                and value.get("type", None) == "constructor"
                and value.get("id", None) is not None
            ):
                self._check_allowed_json_modules(value)
            return super()._reviver(value)

        def loads_typed(self, data: tuple[str, bytes]) -> Any:
            from langgraph.checkpoint.serde.event_hooks import (
                register_serde_event_listener,
            )

            collected: list[Mapping[str, Any]] = []
            token = _active_blocked_events.set(collected)

            def _collect(event: Mapping[str, Any]) -> None:
                # Never raise here: the SDK swallows listener errors into
                # a log line, which would silently drop the signal.
                if _active_blocked_events.get() is collected:
                    if event.get("kind") in _BLOCKED_MSGPACK_KINDS:
                        collected.append(event)

            unregister = register_serde_event_listener(_collect)
            try:
                result = super().loads_typed(data)
            finally:
                # The SDK keeps a listener list, so previously registered
                # listeners stay untouched; only this call listener leaves
                # and the token resets. Neither step raises, so a load
                # failure keeps its original error.
                try:
                    unregister()
                finally:
                    _active_blocked_events.reset(token)
            if collected:
                blocked = ", ".join(_describe_blocked_event(event) for event in collected)
                raise TypeError(f"unregistered checkpoint state type: {blocked}")
            return result

    return _RunScopedSerializer()


def _identity_serializer() -> Any:
    """Constructor-free serializer for the resume identity pass only.

    Empty allowlists mean the SDK permits only its own safe builtins, so
    every registered or extension model is demoted to raw data: this
    serializer cannot build app models and cannot consume serialized
    state. It exists to read identity fields before the run's selected
    codecs are known; the full restore always uses :func:`_serializer`.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    class _IdentityProjectionSerializer(JsonPlusSerializer):
        """Demote lc:2 constructors to raw kwargs; never build a type.

        JSON checkpoints revive through the SDK's langchain Reviver, which
        returns an lc:2 constructor envelope unchanged when the type is
        not in its core allowlist, hiding the identity fields. The SDK's
        own lc:2 revival reads ``value.get("kwargs")``, so returning that
        mapping keeps the same data shape with no constructor call. Nested
        envelopes are already demoted bottom-up by the object hook.
        """

        def _reviver(self, value: dict[str, Any]) -> Any:
            if (
                value.get("lc") == 2
                and value.get("type") == "constructor"
                and isinstance(value.get("id"), list)
            ):
                kwargs = value.get("kwargs")
                if isinstance(kwargs, dict):
                    return kwargs
            return super()._reviver(value)

    return _IdentityProjectionSerializer(allowed_msgpack_modules=[], allowed_json_modules=[])


@asynccontextmanager
async def _open_sqlite_saver(*, path: str, serde: Any):
    """Own the aiosqlite connection lifecycle for one saver instance."""
    from pathlib import Path

    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:  # pragma: no cover - optional-extra failure path
        raise RuntimeError(
            'SQLite checkpointing requires pip install -e ".[orchestration]"'
        ) from exc

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(resolved)
    try:
        yield AsyncSqliteSaver(connection, serde=serde)
    finally:
        await connection.close()


def _resolve_allowlist(registry: StateCodecRegistry | None) -> tuple[tuple[str, str], ...]:
    """One run's serializer allowlist: selection when given, legacy default."""
    if registry is not None:
        return explicit_allowlist(registry)
    return legacy_allowlist()


def build_checkpointer(
    *, enabled: bool = True, registry: StateCodecRegistry | None = None
) -> Any | None:
    """An in-memory saver, or ``None`` when checkpointing is off.

    Durable runs use :func:`open_checkpointer` with the SQLite backend.
    With ``registry`` the saver only restores the package scan plus that
    run's selected codecs; without it the legacy default (scan plus the
    legacy global registry) applies.
    """
    if not enabled:
        return None

    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver(serde=_serializer(_resolve_allowlist(registry), registry))


@asynccontextmanager
async def open_checkpointer(
    *,
    enabled: bool = True,
    backend: str = "memory",
    path: str | None = None,
    registry: StateCodecRegistry | None = None,
):
    """Open a checkpointer with explicit lifecycle ownership.

    SQLite's async saver owns a live connection, so callers must keep this
    context open for history/replay and close it when the runner is done.
    The ``registry`` rule matches :func:`build_checkpointer`.
    """

    if not enabled:
        yield None
        return
    if backend == "memory":
        yield build_checkpointer(enabled=True, registry=registry)
        return
    if backend != "sqlite":
        raise ValueError(f"unsupported checkpoint backend: {backend!r}")
    if not path:
        raise ValueError("sqlite checkpoint backend needs a path")

    async with _open_sqlite_saver(
        path=path, serde=_serializer(_resolve_allowlist(registry), registry)
    ) as saver:
        yield saver


@dataclass(frozen=True)
class CheckpointIdentity:
    """Identity fields a resume needs before its codec selection exists."""

    engagement_id: EngagementId
    mission_id: str | None = None
    component_binding: RuntimeComponentBinding | None = None


@asynccontextmanager
async def open_identity_reader(
    *, enabled: bool = True, backend: str = "memory", path: str | None = None
):
    """Open a constructor-free checkpoint reader for the resume identity pass.

    Same storage and lifecycle as open_checkpointer, but the serde blocks
    every constructor, so registered and extension models come back as raw
    data. Only read_checkpoint_identity may use this reader; the typed
    restore always goes through open_checkpointer with the run's selected
    registry.
    """
    if not enabled:
        yield None
        return
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver(serde=_identity_serializer())
        return
    if backend != "sqlite":
        raise ValueError(f"unsupported checkpoint backend: {backend!r}")
    if not path:
        raise ValueError("sqlite checkpoint backend needs a path")

    async with _open_sqlite_saver(path=path, serde=_identity_serializer()) as saver:
        yield saver


async def read_checkpoint_identity(
    *, enabled: bool = True, backend: str = "memory", path: str | None = None, run_id: str
) -> CheckpointIdentity | None:
    """Read only the identity a resume needs, without any typed restore.

    The checkpoint state is projected to raw data (no constructor runs), so
    a checkpoint written with per-run selected codecs can still be read
    before that selection is known. Missing or tampered identity records
    return None or raise, and the caller refuses the resume; approvals and
    the full runtime are never read here.
    """
    from redteam.core.ids import EngagementId
    from redteam.orchestration.component_binding import RuntimeComponentBinding

    async with open_identity_reader(enabled=enabled, backend=backend, path=path) as saver:
        if saver is None:
            return None
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
        checkpoint = await saver.aget(config)
        if not isinstance(checkpoint, Mapping):
            return None
        values = checkpoint.get("channel_values")
        if not isinstance(values, Mapping):
            return None
        runtime = values.get("runtime")
        if not isinstance(runtime, Mapping):
            return None
        engagement = runtime.get("engagement_id")
        if not isinstance(engagement, str) or not engagement:
            return None
        mission = runtime.get("mission_id")
        binding_raw = runtime.get("component_binding")
        binding = (
            None if binding_raw is None else RuntimeComponentBinding.model_validate(binding_raw)
        )
        return CheckpointIdentity(
            engagement_id=EngagementId(engagement),
            mission_id=None if mission is None else str(mission),
            component_binding=binding,
        )


class CheckpointIdentityError(ValueError):
    """A matching run thread's latest checkpoint identity is unreadable.

    Raised (never skipped) when the LATEST root-namespace checkpoint of a
    thread belonging to the requested run has an invalid runtime mapping,
    a run/instance mismatch, missing engagement, or an unparsable
    binding. Omitting the thread would hide its consumed budget and cause
    a budget reset, so the whole enumeration refuses instead.
    """


@dataclass(frozen=True)
class CheckpointThreadIdentity:
    """Latest identity of one checkpoint thread, read constructor-free."""

    thread_id: str
    run_id: str
    #: None is the legacy singleton thread; otherwise the instance id.
    instance_id: str | None
    engagement_id: EngagementId
    mission_id: str | None = None
    component_binding: RuntimeComponentBinding | None = None


def _split_checkpoint_thread(thread_id: str, run_id: str) -> str | None | bool:
    """Map a checkpoint thread to its instance id for ``run_id``.

    Returns the instance id, None for the legacy singleton thread, or
    False when the thread belongs to another run. The split mirrors
    ``runner._checkpoint_thread`` without importing it (checkpointing stays
    below orchestration wiring).
    """
    if thread_id == run_id:
        return None
    marker = f"{run_id}:instance:"
    if thread_id.startswith(marker) and len(thread_id) > len(marker):
        return thread_id[len(marker):]
    return False


async def list_checkpoint_threads(
    *, enabled: bool = True, backend: str = "memory", path: str | None = None,
    run_id: str,
) -> list[CheckpointThreadIdentity]:
    """Latest identity per checkpoint thread of a run, constructor-free.

    Enumerates the SDK checkpoint threads (``saver.alist`` with no thread
    filter, newest first) and projects each thread's latest runtime to raw
    data -- no model constructor runs, mirroring ``read_checkpoint_identity``.
    Every matching thread counts, including idle ones with no WorkStore
    rows: identities are never derived from the work ledger alone.
    Every matching thread counts, including idle ones with no WorkStore
    rows: identities are never derived from the work ledger alone.

    The LATEST checkpoint of each thread decides it: a thread whose latest
    checkpoint is invalid is refused outright -- an older valid checkpoint
    of the same thread is never selected as a silent rewind (that would be
    unverified replay). Only the root namespace (``checkpoint_ns == ""``)
    is read, so a nested subgraph runtime cannot overshadow the root.
    Storage failures propagate as a bounded class-only ``RuntimeError``,
    never as a silent empty list.

    Durable enumeration is sqlite; the memory backend reader is a fresh
    in-process saver, so cross-process memory resume stays unsupported.
    """
    from redteam.core.ids import EngagementId
    from redteam.orchestration.component_binding import RuntimeComponentBinding

    seen: set[str] = set()
    found: dict[str, CheckpointThreadIdentity] = {}
    try:
        async with open_identity_reader(enabled=enabled, backend=backend, path=path) as saver:
            if saver is None:
                return []
            async for item in saver.alist(None):
                outer = getattr(item, "config", None) or {}
                inner = outer.get("configurable") if isinstance(outer, dict) else None
                if not isinstance(inner, dict) or inner.get("checkpoint_ns", "") != "":
                    continue
                thread_id = inner.get("thread_id")
                if not isinstance(thread_id, str) or thread_id in seen:
                    continue
                # Seen BEFORE parsing: the latest checkpoint decides the
                # thread, refused or not. No older-checkpoint fallback.
                seen.add(thread_id)
                instance = _split_checkpoint_thread(thread_id, str(run_id))
                if instance is False:
                    continue
                checkpoint = getattr(item, "checkpoint", None)
                values = (
                    checkpoint.get("channel_values")
                    if isinstance(checkpoint, Mapping)
                    else None
                )
                runtime = values.get("runtime") if isinstance(values, Mapping) else None
                if not isinstance(runtime, Mapping):
                    raise CheckpointIdentityError(
                        f"unreadable latest runtime for thread {thread_id!r}"
                    )
                if str(runtime.get("run_id", "")) != str(run_id):
                    raise CheckpointIdentityError(
                        f"run mismatch in latest checkpoint of thread {thread_id!r}"
                    )
                raw_instance = runtime.get("instance_id")
                if (raw_instance is None) != (instance is None) or (
                    raw_instance is not None and str(raw_instance) != str(instance)
                ):
                    raise CheckpointIdentityError(
                        f"instance mismatch in latest checkpoint of thread {thread_id!r}"
                    )
                engagement = runtime.get("engagement_id")
                if not isinstance(engagement, str) or not engagement:
                    raise CheckpointIdentityError(
                        f"missing engagement in latest checkpoint of thread {thread_id!r}"
                    )
                mission = runtime.get("mission_id")
                binding_raw = runtime.get("component_binding")
                try:
                    binding = (
                        None
                        if binding_raw is None
                        else RuntimeComponentBinding.model_validate(binding_raw)
                    )
                except Exception as exc:
                    raise CheckpointIdentityError(
                        f"unparsable binding in latest checkpoint of thread {thread_id!r}: "
                        f"{type(exc).__name__}"
                    ) from exc
                found[thread_id] = CheckpointThreadIdentity(
                    thread_id=thread_id,
                    run_id=str(run_id),
                    instance_id=None if instance is None else str(instance),
                    engagement_id=EngagementId(engagement),
                    mission_id=None if mission is None else str(mission),
                    component_binding=binding,
                )
    except CheckpointIdentityError:
        raise
    except Exception as exc:
        raise RuntimeError(f"checkpoint enumeration failed: {type(exc).__name__}") from exc
    return list(found.values())
