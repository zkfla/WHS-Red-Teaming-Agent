"""Card-first tool discovery over the single source per kind (TASK-004).

Four kinds share one card shape but never one registry. Each card is built
from its owning source snapshot at call time, so filtered views can never
become a hidden allowlist and no second registry can drift from discovery:

- mcp_tool: the MCP registry snapshot through the existing narrow
  ToolCardView port (catalog or gateway; both read the same registry).
- local_helper: LocalToolRegistry descriptors (callable runtime-local
  tools). Like catalog tools they are exposed lazily: only helpers whose
  contribution declares bootstrap (the card search and detail navigators)
  stay always-advertised. Contributions may independently declare
  initial_exposure for essential read/Task-return schemas. Other local schemas
  appear after a detail lookup. Native external schema advertisement provides
  the entire eligible catalog when its full input envelope fits the bounded
  budget; otherwise discovery remains lazy. No lookup means full advertisement,
  still subject to the same input bound.
- program: shell programs from two single sources -- (a) the TASK-003
  image manifest for image-provided programs, read only through the
  ShellProgramDiscovery provider, (b) live workroom rows for the trusted
  run/instance subject on explicit program tools. Programs are information
  only (callable=False); they run through shell execution input, never as
  directly callable model tools, and instance installs are never
  auto-promoted into the shared catalog. Card identity is a stable opaque
  id over scope, name, and executable path.
- reference: operator-provisioned ReferenceLibrary entries (read-only
  metadata; content is read through the existing reader path).

Search results are never authorization. Execution permission and input
validity are decided at the real execution boundary (catalog plus
tools_by_role plus Control admission), and a card lookup never grants or
removes eligibility. The six discovery helpers are declared here as
runtime_tool contributions for the assembly to register.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.core.ports.llm import NativeToolSpec
from redteam.core.ports.program_discovery import (
    PROGRAM_ABSENT_CODES,
    ProgramRecord,
    ShellProgramDiscovery,
    program_error_code,
)
from redteam.core.ports.tool_catalog import (
    ToolCard,
    ToolCardKind,
    ToolDetail,
    filter_tool_cards,
    validate_card_kind,
)
from redteam.orchestration.local_tools import (
    LocalToolContext,
    LocalToolDescriptor,
    LocalToolResult,
    runtime_tool_contribution,
)
from redteam.orchestration.methodology import ReferenceEntry, ReferenceLibrary
from redteam.orchestration.native_binding import exposed_tool_name

CARD_SUMMARY_CHARS = 240
MAX_LIST_CARDS = 100


class NativeInputLimit(BaseModel):
    """Canonical UTF-8 input envelope budget, independent of output tokens.

    No operational setting/cache: one default for the exposure owner. Smaller
    budgets are useful to validate the same boundary without large fixtures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_bytes: int = Field(default=256 * 1024, strict=True, gt=0, le=256 * 1024)

    def size(self, *, messages, tools: Sequence[NativeToolSpec], system) -> int:
        return len(json.dumps(
            {"messages": messages, "tools": [asdict(tool) for tool in tools], "system": system},
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))

    def fits(self, **envelope) -> bool:
        return self.size(**envelope) <= self.max_bytes


NATIVE_INPUT_LIMIT = NativeInputLimit()

TOOL_LIST_CARDS_ID = "tool.list_cards"
TOOL_GET_DETAIL_ID = "tool.get_detail"
PROGRAM_LIST_ID = "program.list"
PROGRAM_DESCRIBE_ID = "program.describe"
REFERENCE_LIST_ID = "reference.list"
REFERENCE_DESCRIBE_ID = "reference.describe"

REFERENCE_OWNER_SCOPE = "reference"
LOCAL_OWNER_SCOPE = "local"
LOCAL_SERVER_ID = "local"
PROGRAM_MANIFEST_STATUS_KEY = "program_manifest_status"
WORKROOM_STATUS_KEY = "workroom_status"
PROVIDER_MISSING_NOTE = "shell program provider is not wired (TASK-008)"
NO_SUBJECT_NOTE = "no trusted workroom subject for this run (identity missing)"
LIVE_DEFERRED_NOTE = "instance rows need an explicit program.list (no live query here)"


def _summarize(text: str) -> str:
    summary = (text or "").strip()
    if len(summary) > CARD_SUMMARY_CHARS:
        summary = summary[:CARD_SUMMARY_CHARS].rstrip() + "..."
    return summary


def _trusted_subject(runtime: Any) -> tuple[str, str] | None:
    """Trusted execution identity for instance program lookup, if complete.

    Only the run identity the execution context already owns travels to
    the provider, which derives the workroom key itself with its own
    workroom_key_for; instance identity is required because there is no
    legacy mapping. Caller-built key strings never cross this boundary,
    and model input is never consulted.
    """

    run_id = str(getattr(runtime, "run_id", "") or "").strip()
    instance_id = getattr(runtime, "instance_id", None)
    if not run_id or instance_id is None or not str(instance_id).strip():
        return None
    return (run_id, str(instance_id))


def local_helper_card(descriptor: LocalToolDescriptor) -> ToolCard:
    """Project one runtime-local helper to its display card.

    Local helpers are callable through the provider tools parameter, so
    callable is True; live availability is trivially healthy because the
    handler runs in-process once registered.
    """

    identity = str(descriptor.tool_id)
    return ToolCard(
        tool_id=ToolId(identity),
        server_id=LOCAL_SERVER_ID,
        tool_name=identity,
        kind="local_helper",
        owner_scope=LOCAL_OWNER_SCOPE,
        title=identity,
        summary=_summarize(descriptor.description),
        capability_tags=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
        callable=True,
    )


def _program_card_id(*, scope: str, name: str, executable: str) -> str:
    """Stable opaque identity for one enumerated row.

    The digest covers scope, name, and executable path, so identical names
    in one scope stay distinct. Selection matches this id exactly; no part
    of it is ever parsed back out.
    """

    digest = hashlib.sha256(
        chr(0).join([scope, name, executable]).encode("utf-8")
    ).hexdigest()[:12]
    return "program." + scope + "." + name + "." + digest


def program_card(
    *,
    name: str,
    executable: str,
    version: str | None,
    provenance: str | None,
    owner_scope: str,
    source_id: str,
) -> ToolCard:
    """Project one enumerated executable to its information-only card.

    The row is consumed structurally from the TASK-003 provider and names
    a real executable path, never a package-to-command guess. The owner
    scope travels verbatim ("image" or "instance") so an instance install
    can never be mistaken for shared-catalog data. The card server label
    comes from the provider source_id passed in by the caller, never from
    a central constant. Card identity is an opaque stable id over scope,
    name, and executable path, so even rows sharing a scope and name keep
    distinct selectable cards. Programs are never callable model tools.
    """

    clean = str(name or "").strip()
    binary = str(executable or "").strip()
    tag = str(version or "").strip()
    label = clean if not tag else clean + " " + tag
    origin = str(provenance or "").strip() or owner_scope
    scope = str(owner_scope or "").strip() or "instance"
    if binary:
        detail = label + " at " + binary + " (" + origin + ")"
    else:
        detail = label + " (" + origin + ")"
    return ToolCard(
        tool_id=ToolId(_program_card_id(scope=scope, name=clean, executable=binary)),
        server_id=str(source_id),
        tool_name=clean,
        kind="program",
        owner_scope=owner_scope,
        title=label,
        summary=_summarize(detail),
        capability_tags=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.UNKNOWN,
        callable=False,
    )


def reference_card(entry: ReferenceEntry) -> ToolCard:
    """Project one operator-provisioned reference entry to its card.

    The card is metadata only (title, domains, provenance); the content
    stays unread until the model uses the existing reader path. Domains
    are search text, not capability tags, so capability filtering keeps
    the MCP tag vocabulary exact.
    """

    domains = [str(item) for item in entry.domains]
    return ToolCard(
        tool_id=ToolId("reference." + entry.reference_id),
        server_id=str(entry.source_id),
        tool_name=str(entry.reference_id),
        kind="reference",
        owner_scope=REFERENCE_OWNER_SCOPE,
        title=str(entry.title),
        summary=_summarize(str(entry.title) + " [" + ", ".join(domains) + "]"),
        capability_tags=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.UNKNOWN,
        callable=False,
    )


def _local_haystack(descriptor: LocalToolDescriptor) -> str:
    return " ".join([str(descriptor.tool_id), descriptor.description or ""])


def _program_haystack(
    *,
    name: str,
    executable: str,
    version: str | None,
    provenance: str | None,
    owner_scope: str,
) -> str:
    return " ".join(
        [
            str(name or ""),
            str(executable or ""),
            str(version or ""),
            str(provenance or ""),
            owner_scope,
        ]
    )


def _reference_haystack(entry: ReferenceEntry) -> str:
    return " ".join(
        [
            str(entry.reference_id),
            str(entry.title),
            str(entry.source_id),
            str(entry.path),
            str(entry.review_caveat),
            *[str(item) for item in entry.domains],
        ]
    )


def snapshot_cards(
    cards: Sequence[ToolCard],
    haystacks: Sequence[str],
    *,
    kind: ToolCardKind | None = None,
    capability: str | None = None,
    query: str | None = None,
    include_unhealthy: bool = False,
) -> list[ToolCard]:
    """Filter one kind snapshot with the shared card rule (core-owned).

    Kinds without capability tags (locals, programs, references) simply
    match nothing when a capability is given; the MCP tag vocabulary stays
    exact and is never widened with foreign tags.
    """

    return filter_tool_cards(
        list(zip(cards, haystacks, strict=True)),
        kind=kind,
        capability=capability,
        query=query,
        include_unhealthy=include_unhealthy,
    )


def card_view_for(deps: Any) -> Any | None:
    """First card-capable port on deps; both read the same source snapshot.

    The catalog and the gateway project cards from the same registry, so
    either port serves discovery. None means no card source is wired and
    callers must say so explicitly instead of inventing cards.
    """

    for candidate in (getattr(deps, "tool_catalog", None), getattr(deps, "tool_gateway", None)):
        if candidate is not None and hasattr(candidate, "list_cards") and hasattr(
            candidate, "get_detail"
        ):
            return candidate
    return None


def _project_program_rows(
    rows: Sequence[ProgramRecord], *, source_id: str
) -> list[tuple[ToolCard, str]]:
    """Project enumerated rows to card plus full-text pairs; no filtering here."""

    pairs: list[tuple[ToolCard, str]] = []
    for row in rows:
        scope = str(row.owner_scope or "").strip() or "instance"
        pairs.append(
            (
                program_card(
                    name=row.name,
                    executable=row.executable,
                    version=row.version,
                    provenance=row.provenance,
                    owner_scope=scope,
                    source_id=source_id,
                ),
                _program_haystack(
                    name=row.name,
                    executable=row.executable,
                    version=row.version,
                    provenance=row.provenance,
                    owner_scope=scope,
                ),
            )
        )
    return pairs


async def collect_program_rows(
    provider: ShellProgramDiscovery | None,
    runtime: Any | None,
    *,
    include_live: bool = True,
) -> tuple[list[ProgramRecord], dict[str, Any]]:
    """Raw image plus trusted-instance rows with explicit source status.

    Filtering stays with the caller so card search and context assembly
    share the same rows. A failing source degrades to an explicit status
    with the other source intact, never to silent emptiness. The live
    workroom query runs only when include_live is true: passive context
    assembly passes false so no model turn pays for docker exec and pip
    list calls, while explicit program tools keep the live rows. A
    follow-up may revisit the per-turn refresh policy; no cache registry
    is added here.
    """

    extra: dict[str, Any] = {}
    if provider is None:
        extra[PROGRAM_MANIFEST_STATUS_KEY] = PROVIDER_MISSING_NOTE
        extra[WORKROOM_STATUS_KEY] = PROVIDER_MISSING_NOTE
        return [], extra
    rows: list[ProgramRecord] = []
    try:
        rows.extend(await provider.list_image_programs())
        extra[PROGRAM_MANIFEST_STATUS_KEY] = "published"
    except Exception as exc:
        code = program_error_code(exc)
        extra[PROGRAM_MANIFEST_STATUS_KEY] = "image read failed: " + code
    if not include_live:
        extra[WORKROOM_STATUS_KEY] = LIVE_DEFERRED_NOTE
        return rows, extra
    subject = _trusted_subject(runtime)
    if subject is None:
        extra[WORKROOM_STATUS_KEY] = NO_SUBJECT_NOTE
    else:
        try:
            rows.extend(
                await provider.list_instance_programs(
                    run_id=subject[0], instance_id=subject[1]
                )
            )
            extra[WORKROOM_STATUS_KEY] = "listed"
        except Exception as exc:
            code = program_error_code(exc)
            if code in PROGRAM_ABSENT_CODES:
                extra[WORKROOM_STATUS_KEY] = "not_running"
            else:
                extra[WORKROOM_STATUS_KEY] = "workroom lookup failed: " + code
    return rows, extra


async def program_cards_for_query(
    provider: ShellProgramDiscovery | None,
    *,
    query: str | None = None,
    runtime: Any | None = None,
    include_live: bool = True,
) -> tuple[list[ToolCard], dict[str, Any]]:
    """Gather image plus trusted-instance program cards, with source status.

    Query-only filtering for context assembly and program listing; richer
    filters stay with the card handler. Rows come from collect_program_rows,
    so the trusted-subject rule holds on every path. Passive callers pass
    include_live=False to skip the docker-backed workroom query.
    """

    rows, extra = await collect_program_rows(provider, runtime, include_live=include_live)
    if provider is None:
        return [], extra
    pairs = _project_program_rows(rows, source_id=provider.source_id)
    cards = [card for card, _hay in pairs]
    haystacks = [hay for _card, hay in pairs]
    return snapshot_cards(cards, haystacks, query=query), extra


def reference_cards(library: ReferenceLibrary | None) -> list[ToolCard]:
    """Cards for operator-provisioned reference entries, if any."""

    if library is None:
        return []
    return [reference_card(entry) for entry in library.entries]


async def discovery_cards_for_context(
    *,
    view: Any | None,
    eligible_ids: Collection[str],
    local_descriptors: Sequence[LocalToolDescriptor] = (),
    provider: ShellProgramDiscovery | None = None,
    runtime: Any | None = None,
    library: ReferenceLibrary | None = None,
    include_live_programs: bool = False,
) -> tuple[list[ToolCard], dict[str, Any]]:
    """Shallow cards for the model context: no schemas, capped briefing.

    MCP cards cover exactly the role-eligible set (the same pool the
    engine already computed). The passive briefing is capped at
    MAX_LIST_CARDS with totals, per-kind counts, and search guidance, so
    a large program set cannot bloat every turn; bindings and permissions
    stay complete because they never read this list. Live instance rows
    stay out unless the caller explicitly opts in. Returns the page plus
    the program source status so a broken manifest stays visible instead
    of degrading to silent empty cards.
    """

    eligible = {str(item) for item in eligible_ids}
    cards: list[ToolCard] = []
    if view is not None:
        for card in view.list_cards():
            if str(card.tool_id) in eligible:
                cards.append(card)
    cards.extend(local_helper_card(item) for item in local_descriptors)
    programs, program_status = await program_cards_for_query(
        provider, runtime=runtime, include_live=include_live_programs
    )
    cards.extend(programs)
    cards.extend(reference_cards(library))
    ordered = sorted(cards, key=lambda item: (str(item.kind), str(item.tool_id)))
    page = ordered[:MAX_LIST_CARDS]
    counts: dict[str, int] = {}
    for card in ordered:
        counts[str(card.kind)] = counts.get(str(card.kind), 0) + 1
    status: dict[str, Any] = dict(program_status)
    status["cards_total"] = len(ordered)
    status["cards_truncated"] = len(ordered) > len(page)
    status["cards_counts"] = counts
    if status["cards_truncated"]:
        status["cards_guidance"] = (
            "Passive card briefing is capped; enumerate the full set with "
            "tool.list_cards (kind plus query plus offset/limit) and read "
            "schemas with tool.get_detail."
        )
    return (page, status)


def _string_arg(tool_input, name):
    value = tool_input.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _page_args(tool_input):
    """Caller paging as (offset, limit), or an error string when out of range."""

    offset = tool_input.get("offset", 0)
    limit = tool_input.get("limit", MAX_LIST_CARDS)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return "bad offset"
    if isinstance(limit, bool) or not isinstance(limit, int):
        return "bad limit"
    if not 1 <= limit <= MAX_LIST_CARDS:
        return "bad limit"
    return (offset, limit)


def _cards_payload(cards, *, offset=0, limit=MAX_LIST_CARDS, extra=None):
    page = cards[offset : offset + limit]
    rest = len(cards) - (offset + len(page))
    payload = {
        "cards": [card.model_dump(mode="json") for card in page],
        "total": len(cards),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + len(page) if rest > 0 else None,
        "truncated": rest > 0,
    }
    if extra:
        payload.update(extra)
    return payload


def _page_properties() -> dict[str, Any]:
    return {
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_CARDS},
    }


def _tool_list_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "capability": {"type": "string"},
            "query": {"type": "string"},
            "include_unhealthy": {"type": "boolean"},
            **_page_properties(),
        },
        "additionalProperties": False,
    }


async def tool_list_cards_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    kind = _string_arg(tool_input, "kind")
    if kind is not None:
        try:
            validate_card_kind(cast(ToolCardKind, kind))
        except ValueError:
            return LocalToolResult(ok=False, error="unknown tool card kind")
    capability = _string_arg(tool_input, "capability")
    query = _string_arg(tool_input, "query")
    include_unhealthy = bool(tool_input.get("include_unhealthy", False))
    page = _page_args(tool_input)
    if isinstance(page, str):
        return LocalToolResult(ok=False, error=page)
    offset, limit = page
    view = card_view_for(deps)
    mcp_cards: list[ToolCard] = []
    if (kind is None or kind == "mcp_tool") and view is not None:
        mcp_cards = view.list_cards(
            capability=capability, query=query, include_unhealthy=include_unhealthy
        )
        from redteam.orchestration.agents.tool_access import specialist_tool_ids

        selected = await specialist_tool_ids(
            deps, context.runtime, include_unhealthy=include_unhealthy,
        )
        if selected is not None:
            mcp_cards = [card for card in mcp_cards if str(card.tool_id) in selected]
    locals_cards: list[ToolCard] = []
    if kind is None or kind == "local_helper":
        registry = getattr(deps, "local_tools", None)
        descriptors = list(registry.descriptors()) if registry is not None else []
        only = kind if kind == "local_helper" else None
        locals_cards = snapshot_cards(
            [local_helper_card(item) for item in descriptors],
            [_local_haystack(item) for item in descriptors],
            kind=only,
            capability=capability,
            query=query,
            include_unhealthy=include_unhealthy,
        )
    program_cards: list[ToolCard] = []
    program_extra: dict[str, Any] = {}
    if kind is None or kind == "program":
        provider = getattr(deps, "shell_programs", None)
        rows, program_extra = await collect_program_rows(
            provider, getattr(context, "runtime", None)
        )
        if provider is None:
            pairs = []
        else:
            pairs = _project_program_rows(rows, source_id=provider.source_id)
        only = kind if kind == "program" else None
        program_cards = snapshot_cards(
            [card for card, _hay in pairs],
            [hay for _card, hay in pairs],
            kind=only,
            capability=capability,
            query=query,
            include_unhealthy=include_unhealthy,
        )
    reference_hits: list[ToolCard] = []
    if kind is None or kind == "reference":
        library = getattr(deps, "methodology_library", None)
        refs = list(library.entries) if library is not None else []
        only = kind if kind == "reference" else None
        reference_hits = snapshot_cards(
            [reference_card(item) for item in refs],
            [_reference_haystack(item) for item in refs],
            kind=only,
            capability=capability,
            query=query,
            include_unhealthy=include_unhealthy,
        )
    merged = sorted(
        [*mcp_cards, *locals_cards, *program_cards, *reference_hits],
        key=lambda item: (str(item.kind), str(item.tool_id)),
    )
    extra: dict[str, Any] = {}
    if kind is None or kind == "program":
        extra.update(program_extra)
    return LocalToolResult(
        ok=True, payload=_cards_payload(merged, offset=offset, limit=limit, extra=extra)
    )


async def tool_get_detail_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    wanted = _string_arg(tool_input, "tool_id")
    if wanted is None:
        return LocalToolResult(ok=False, error="tool_id is required")
    registry = getattr(deps, "local_tools", None)
    if registry is not None:
        for descriptor in registry.descriptors():
            if str(descriptor.tool_id) != wanted:
                continue
            schema = descriptor.input_schema if isinstance(descriptor.input_schema, dict) else {}
            props = schema.get("properties")
            properties = props if isinstance(props, dict) else {}
            required = [str(item) for item in schema.get("required") or []]
            detail = ToolDetail(
                card=local_helper_card(descriptor),
                description=descriptor.description,
                input_schema=schema,
                output_schema=None,
                target_types=[],
                operations=[],
                required_arguments=required,
                optional_arguments=[str(key) for key in properties if str(key) not in required],
            )
            return LocalToolResult(ok=True, payload=detail.model_dump(mode="json"))
    view = card_view_for(deps)
    if view is None:
        return LocalToolResult(ok=False, error="tool detail source is not wired")
    from redteam.orchestration.agents.tool_access import specialist_tool_ids

    selected = await specialist_tool_ids(deps, context.runtime)
    if selected is not None and wanted not in selected:
        return LocalToolResult(ok=False, error="tool_not_visible_to_current_role")
    try:
        found = view.get_detail(ToolId(wanted))
    except (ValueError, TypeError):
        return LocalToolResult(ok=False, error="unknown tool")
    if found is None:
        return LocalToolResult(ok=False, error="unknown tool")
    return LocalToolResult(ok=True, payload=found.model_dump(mode="json"))


def _program_list_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            **_page_properties(),
        },
        "additionalProperties": False,
    }


async def program_list_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    query = _string_arg(tool_input, "query")
    page = _page_args(tool_input)
    if isinstance(page, str):
        return LocalToolResult(ok=False, error=page)
    offset, limit = page
    cards, extra = await program_cards_for_query(
        getattr(deps, "shell_programs", None),
        query=query,
        runtime=getattr(context, "runtime", None),
    )
    return LocalToolResult(
        ok=True, payload=_cards_payload(cards, offset=offset, limit=limit, extra=extra)
    )


_SCOPE_RANK = {"instance": 0, "image": 1}


def _row_card_id(row: ProgramRecord) -> str:
    """Computed identity for one provider row.

    Same normalization as program_card, so describe matches exactly the id
    the card carries without parsing any dot segments back out.
    """

    scope = str(row.owner_scope or "").strip() or "instance"
    return _program_card_id(
        scope=scope,
        name=str(row.name).strip(),
        executable=str(row.executable or "").strip(),
    )


def _select_program_row(
    rows: Sequence[ProgramRecord],
    *,
    tool_id: str | None = None,
    name: str | None = None,
    owner_scope: str | None = None,
) -> tuple[ProgramRecord, str]:
    """Pick one provider row; never silently return the wrong scope.

    A card id selects by exact computed-identity match. A name plus an
    explicit scope selects exactly. A bare name shared across scopes
    resolves to the effective (PATH-preferred) row and says so; several
    rows at the winning rank stay ambiguous and raise. Unknown identities
    raise ValueError with a fixed message.
    """

    if tool_id is not None:
        for row in rows:
            if _row_card_id(row) != tool_id:
                continue
            if owner_scope is not None and str(row.owner_scope) != owner_scope:
                continue
            return (row, "exact")
        raise ValueError("unknown program")
    if name is None:
        raise ValueError("tool_id or name is required")
    candidates = [row for row in rows if str(row.name) == name]
    if owner_scope is not None:
        candidates = [row for row in candidates if str(row.owner_scope) == owner_scope]
    if not candidates:
        raise ValueError("unknown program")
    ranked = sorted(candidates, key=lambda row: _SCOPE_RANK.get(str(row.owner_scope), 99))
    top_rank = _SCOPE_RANK.get(str(ranked[0].owner_scope), 99)
    tied = [row for row in ranked if _SCOPE_RANK.get(str(row.owner_scope), 99) == top_rank]
    if len(tied) > 1:
        raise ValueError("ambiguous program: specify tool_id")
    if len(ranked) == 1:
        return (ranked[0], "exact")
    return (ranked[0], "effective-path-preference")


async def program_describe_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    provider = getattr(deps, "shell_programs", None)
    rows, extra = await collect_program_rows(
        provider, getattr(context, "runtime", None)
    )
    if provider is None:
        return LocalToolResult(ok=False, error="unknown program")
    try:
        match, resolution = _select_program_row(
            rows,
            tool_id=_string_arg(tool_input, "tool_id"),
            name=_string_arg(tool_input, "name"),
            owner_scope=_string_arg(tool_input, "owner_scope"),
        )
    except ValueError as exc:
        return LocalToolResult(ok=False, error=str(exc))
    scope = str(match.owner_scope or "").strip() or "instance"
    card = program_card(
        name=match.name,
        executable=match.executable,
        version=match.version,
        provenance=match.provenance,
        owner_scope=scope,
        source_id=provider.source_id,
    )
    return LocalToolResult(
        ok=True,
        payload={
            "card": card.model_dump(mode="json"),
            "tool_id": str(card.tool_id),
            "name": str(match.name),
            "executable": str(match.executable or ""),
            "version": str(match.version or ""),
            "provenance": str(match.provenance or ""),
            "owner_scope": scope,
            "resolution": resolution,
            "available_scopes": sorted(
                {str(row.owner_scope) for row in rows if str(row.name) == str(match.name)}
            ),
            "run_with": (
                "shell_run or shell_job_start script input (TASK-003 builtin); "
                "programs are not callable tool names",
            ),
            "program_manifest_status": extra.get(PROGRAM_MANIFEST_STATUS_KEY, ""),
            "workroom_status": extra.get(WORKROOM_STATUS_KEY, ""),
        },
    )


async def reference_list_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    query = _string_arg(tool_input, "query")
    domain = _string_arg(tool_input, "domain")
    page = _page_args(tool_input)
    if isinstance(page, str):
        return LocalToolResult(ok=False, error=page)
    offset, limit = page
    library = getattr(deps, "methodology_library", None)
    entries = list(library.entries) if library is not None else []
    if domain is not None:
        entries = [item for item in entries if domain in [str(d) for d in item.domains]]
    cards = snapshot_cards(
        [reference_card(item) for item in entries],
        [_reference_haystack(item) for item in entries],
        query=query,
    )
    return LocalToolResult(
        ok=True, payload=_cards_payload(cards, offset=offset, limit=limit)
    )


async def reference_describe_handler(
    tool_input: dict[str, Any], context: LocalToolContext
) -> LocalToolResult:
    deps = getattr(context, "deps", None)
    wanted = _string_arg(tool_input, "reference_id")
    if wanted is None:
        return LocalToolResult(ok=False, error="reference_id is required")
    library = getattr(deps, "methodology_library", None)
    entries = list(library.entries) if library is not None else []
    match = next((item for item in entries if str(item.reference_id) == wanted), None)
    if match is None:
        return LocalToolResult(ok=False, error="unknown reference")
    contract = match.read_contract
    return LocalToolResult(ok=True, payload={
        "card": reference_card(match).model_dump(mode="json"),
        "reference_id": str(match.reference_id),
        "source_id": str(match.source_id),
        "source_commit": str(match.source_commit),
        "path": str(match.path),
        "sha256": str(match.sha256),
        "title": str(match.title),
        "domains": [str(item) for item in match.domains],
        "review_caveat": str(match.review_caveat),
        "trust": "untrusted_reference",
        "review_status": "reviewed_reference_only",
        "execution_permission": False,
        "read_with": {
            "reader_tool_id": str(library.reader_tool_id) if library is not None else "",
            "asset_ref": str(library.asset_ref) if library is not None else "",
            "capability": str(contract.capability),
            "operation": str(contract.operation),
        },
    })


def _descriptor(
    tool_id: str,
    description: str,
    schema: dict[str, Any],
    contribution_id: str,
    *,
    bootstrap: bool = False,
) -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=tool_id,
        description=description,
        input_schema=schema,
        contribution_id=contribution_id,
        bootstrap=bootstrap,
    )


def bootstrap_local_ids(descriptors: Sequence[LocalToolDescriptor]) -> list[str]:
    """Tool ids the model node always advertises, from provider metadata.

    Only helpers whose contribution declares bootstrap (the discovery
    navigators) stay permanently exposed; every other helper is reachable
    through card and detail lookup. No central name list is consulted.
    """

    return [str(item.tool_id) for item in descriptors if item.bootstrap]


def tool_list_cards_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        TOOL_LIST_CARDS_ID,
        "Search shallow tool cards by kind (mcp_tool, local_helper, program, reference), "
        "capability, or keywords. Cards carry no schemas and never grant permission; use "
        "tool.get_detail for the full contract. Execution stays subject to role eligibility "
        "and approval.",
        _tool_list_schema(),
        "tool-list-cards",
        bootstrap=True,
    )


def tool_get_detail_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        TOOL_GET_DETAIL_ID,
        "Read the full contract (description plus input schema) for one callable tool by "
        "tool_id. The schema is exposed for calls on the following turn. Detail lookup "
        "never grants permission.",
        {
            "type": "object",
            "properties": {"tool_id": {"type": "string", "minLength": 1}},
            "required": ["tool_id"],
            "additionalProperties": False,
        },
        "tool-get-detail",
        bootstrap=True,
    )


def program_list_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        PROGRAM_LIST_ID,
        "List shell programs from the image manifest, optionally plus live instance installs "
        "for one workroom_key. Information only: programs run through shell execution input, "
        "never as callable tool names, and workroom installs are never shared-catalog "
        "programs.",
        _program_list_schema(),
        "program-list",
    )


def program_describe_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        PROGRAM_DESCRIBE_ID,
        "Describe one shell program by card identity (the opaque tool_id copied from "
        "program.list) or by name with an optional owner scope. A bare shadowed name "
        "resolves to the effective PATH-preferred row and says so. Information only, "
        "never a call grant.",
        {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "owner_scope": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
        "program-describe",
    )


def reference_list_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        REFERENCE_LIST_ID,
        "List operator-provisioned reference bundles by domain or keywords. Metadata only; "
        "content is read through the existing reader path and stays untrusted data.",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "query": {"type": "string"},
                **_page_properties(),
            },
            "additionalProperties": False,
        },
        "reference-list",
    )


def reference_describe_descriptor() -> LocalToolDescriptor:
    return _descriptor(
        REFERENCE_DESCRIBE_ID,
        "Describe one reference bundle by reference_id: provenance, review caveats, and how "
        "to read it. Metadata only, never content; no execution grant.",
        {
            "type": "object",
            "properties": {"reference_id": {"type": "string", "minLength": 1}},
            "required": ["reference_id"],
            "additionalProperties": False,
        },
        "reference-describe",
    )


def _contribution(contribution_id: str, make_descriptor: Any, handler: Any) -> Any:
    def factory(wiring: Any) -> Any:
        return make_descriptor(), handler

    return runtime_tool_contribution(contribution_id=contribution_id, factory=factory)


def discovery_tool_contributions() -> tuple[Any, ...]:
    """Declare the six discovery helpers for the pluggy assembly (TASK-008)."""

    return (
        _contribution("tool-list-cards", tool_list_cards_descriptor, tool_list_cards_handler),
        _contribution("tool-get-detail", tool_get_detail_descriptor, tool_get_detail_handler),
        _contribution("program-list", program_list_descriptor, program_list_handler),
        _contribution("program-describe", program_describe_descriptor, program_describe_handler),
        _contribution("reference-list", reference_list_descriptor, reference_list_handler),
        _contribution(
            "reference-describe", reference_describe_descriptor, reference_describe_handler
        ),
    )


DETAIL_TOOL_IDS = frozenset({TOOL_GET_DETAIL_ID})


def discovery_lookup_wired(local_tool_ids: Collection[str]) -> bool:
    """Whether the card/detail lookup path is available to the model.

    Lazy advertisement is only meaningful when the model can resolve a
    card to a schema through tool.get_detail. Runs without that helper
    (legacy fixtures, unwired assembly) cannot discover anything, so the
    caller must advertise every eligible tool instead of stranding them.
    """

    return TOOL_GET_DETAIL_ID in {str(item) for item in local_tool_ids}


def build_native_bindings(
    *,
    catalog_tool_ids: Collection[str],
    local_tool_ids: Collection[str],
) -> dict[str, str]:
    seen: dict[str, str] = {}
    for identity in catalog_tool_ids:
        seen[exposed_tool_name(str(identity))] = str(identity)
    for identity in local_tool_ids:
        seen[exposed_tool_name(str(identity))] = str(identity)
    return seen


def detail_fetched_tool_ids(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    bindings: Mapping[str, str],
    detail_tool_ids: Collection[str] = DETAIL_TOOL_IDS,
) -> list[str]:
    wanted_names = {name for name, identity in bindings.items() if identity in detail_tool_ids}
    ok_by_call: dict[str, bool] = {}
    for message in messages or []:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            body = json.loads(content) if isinstance(content, str) else {}
        except ValueError:
            continue
        if isinstance(body, dict):
            ok_by_call[str(message.get("tool_call_id"))] = bool(body.get("ok"))
    fetched: list[str] = []
    for message in messages or []:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "") not in wanted_names:
                continue
            if not ok_by_call.get(str(call.get("id"))):
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                continue
            target = arguments.get("tool_id") if isinstance(arguments, dict) else None
            if target and str(target) not in fetched:
                fetched.append(str(target))
    return fetched


def select_advertised_tool_ids(
    *,
    eligible_ids: Collection[str],
    fetched_ids: Collection[str],
    local_tool_ids: Collection[str] = (),
) -> list[str]:
    eligible = {str(item) for item in eligible_ids}
    local = {str(item) for item in local_tool_ids}
    return sorted({str(item) for item in fetched_ids} & eligible - local)
