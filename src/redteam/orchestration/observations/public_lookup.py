"""Reference-only mappers for public lookup tools.

Search results and page reads mention addresses that later reads need to
revisit. These mappers turn exactly those addresses into reference
candidates and nothing else: no nodes, no edges, no findings, no facts.
Only absolute URL strings actually present in the preserved output become
entries, each with the exact raw location. Joined or repaired addresses
are never emitted here; when a joined address differs from its raw value
the raw value is what travels, and only when it is itself absolute.
"""

from __future__ import annotations

from typing import Any

from redteam.core.contracts.target import AnyTarget, UrlTarget
from redteam.core.contracts.tool import ToolResult
from redteam.orchestration.observations.mapper import GraphMutation
from redteam.orchestration.observations.reference_scan import (
    TargetReference,
    is_absolute_url,
)

__all__ = [
    "map_public_search_results",
    "map_public_page_read",
]


def _origin_ids(result: ToolResult) -> tuple[str | None, str | None]:
    """The origin own identifiers every entry must echo, never invent."""
    try:
        action_id = str(result.action_id) if result.action_id is not None else None
    except Exception:
        action_id = None
    try:
        invocation_id = str(result.invocation_id) if result.invocation_id is not None else None
    except Exception:
        invocation_id = None
    return action_id, invocation_id


def _draft(
    url: str, raw_path: list[str | int], action_id: str | None, invocation_id: str | None
) -> TargetReference | None:
    if not is_absolute_url(url):
        return None
    try:
        return TargetReference(
            target=UrlTarget(url=url),
            raw_path=list(raw_path),
            source_action_id=action_id,
            source_invocation_id=invocation_id,
        )
    except Exception:
        return None


def map_public_search_results(
    *,
    target: AnyTarget,
    arguments: dict[str, Any],
    result: ToolResult,
) -> GraphMutation:
    """Draft one reference per absolute result URL actually returned.

    Reads the provider-neutral search shape (items with url fields) as
    plain JSON: only items whose url is an absolute URL string present in
    the output become candidates, with the exact item position as
    raw_path. Titles and snippets never become references.
    """
    _ = (target, arguments)
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    items = result.output.get("items")
    if not isinstance(items, list):
        return GraphMutation()
    action_id, invocation_id = _origin_ids(result)
    references: list[TargetReference] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or url in seen:
            continue
        draft = _draft(url, ["output", "items", index, "url"], action_id, invocation_id)
        if draft is None:
            continue
        seen.add(url)
        references.append(draft)
    return GraphMutation(target_references=references)


def map_public_page_read(
    *,
    target: AnyTarget,
    arguments: dict[str, Any],
    result: ToolResult,
) -> GraphMutation:
    """Draft references for page-read destinations actually reported.

    The reader returns one request outcome: requested_url, final_url and
    an optional redirect_url. Only absolute URLs actually present become
    candidates, with their exact field positions as raw_path. A final
    URL equal to the requested URL repeats what the action already knew,
    so only a differing destination is drafted; a redirect field the
    output does not carry is simply absent, never synthesized.
    """
    _ = (target, arguments)
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    requested = output.get("requested_url")
    candidates: list[tuple[str, list[str | int]]] = []
    final_url = output.get("final_url")
    if isinstance(final_url, str) and final_url != requested:
        candidates.append((final_url, ["output", "final_url"]))
    redirect_url = output.get("redirect_url")
    if isinstance(redirect_url, str):
        candidates.append((redirect_url, ["output", "redirect_url"]))
    action_id, invocation_id = _origin_ids(result)
    references: list[TargetReference] = []
    seen: set[str] = set()
    for url, raw_path in candidates:
        if url in seen:
            continue
        draft = _draft(url, raw_path, action_id, invocation_id)
        if draft is None:
            continue
        seen.add(url)
        references.append(draft)
    return GraphMutation(target_references=references)
