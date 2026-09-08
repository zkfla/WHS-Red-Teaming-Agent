from __future__ import annotations

from pathlib import Path

from redteam_core.acquisition.boundary import ArtifactBoundary
from redteam_core.acquisition.models import (
    AcquisitionBudget,
    AcquisitionBudgetExceeded,
    AcquisitionError,
    ArtifactEntry,
    ArtifactInventory,
    ArtifactOversizeError,
    ArtifactReadResult,
    ArtifactSearchResult,
    SearchHit,
)


def _normalize_suffix(suffix: str | None) -> str | None:
    if suffix is None:
        return None
    cleaned = suffix.strip().casefold()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def inventory_artifacts(
    boundary: ArtifactBoundary,
    budget: AcquisitionBudget,
    *,
    suffix: str | None = None,
    path_contains: str | None = None,
    limit: int | None = None,
) -> ArtifactInventory:
    requested_limit = budget.limits.max_inventory_results if limit is None else limit
    if requested_limit < 1:
        raise ValueError("inventory limit must be positive")
    effective_limit = min(
        requested_limit,
        budget.limits.max_inventory_results,
    )
    wanted_suffix = _normalize_suffix(suffix)
    path_filter = path_contains or ""
    if len(path_filter) > 1_000:
        raise ValueError("path filter exceeds 1000 characters")
    needle = path_filter.casefold()
    entries: list[ArtifactEntry] = []
    truncated = False
    for path in boundary.iter_files():
        budget.consume(scanned_files=1)
        relative = boundary.relative_ref(path)
        if wanted_suffix is not None and path.suffix.casefold() != wanted_suffix:
            continue
        if needle and needle not in relative.casefold():
            continue
        if len(entries) >= effective_limit:
            truncated = True
            break
        entries.append(ArtifactEntry(relative_path=relative, size_bytes=path.stat().st_size))
    return ArtifactInventory(entries=tuple(entries), truncated=truncated)


def _read_bounded_bytes(path: Path, budget: AcquisitionBudget) -> bytes:
    initial_size = path.stat().st_size
    remaining_bytes = budget.limits.max_total_bytes_read - budget.bytes_read
    if initial_size > budget.limits.max_single_file_bytes:
        raise ArtifactOversizeError(
            "artifact exceeds max_single_file_bytes: "
            f"{initial_size} > {budget.limits.max_single_file_bytes}"
        )
    if initial_size > remaining_bytes:
        raise AcquisitionBudgetExceeded("acquisition byte budget exceeded")
    with path.open("rb") as handle:
        raw = handle.read(initial_size)
    final_size = path.stat().st_size
    if final_size != initial_size or len(raw) != initial_size:
        raise AcquisitionError("artifact changed while it was being read")
    budget.consume(bytes_read=len(raw))
    return raw


def read_artifact_text(
    boundary: ArtifactBoundary,
    budget: AcquisitionBudget,
    relative_path: str,
    *,
    max_characters: int | None = None,
) -> ArtifactReadResult:
    path = boundary.resolve(relative_path)
    if not path.is_file():
        raise ValueError("artifact path is not a file")
    requested_cap = budget.limits.max_read_characters if max_characters is None else max_characters
    if requested_cap < 1:
        raise ValueError("read character limit must be positive")
    cap = min(requested_cap, budget.limits.max_read_characters)
    budget.consume(scanned_files=1)
    raw = _read_bounded_bytes(path, budget)
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > cap
    return ArtifactReadResult(
        relative_path=boundary.relative_ref(path),
        content=text[:cap],
        size_bytes=len(raw),
        truncated=truncated,
    )


def search_artifact_text(
    boundary: ArtifactBoundary,
    budget: AcquisitionBudget,
    query: str,
    *,
    suffix: str | None = None,
    limit: int | None = None,
) -> ArtifactSearchResult:
    if not query.strip():
        raise ValueError("search query must be non-empty")
    if len(query) > 1_000:
        raise ValueError("search query exceeds 1000 characters")
    requested_limit = budget.limits.max_search_results if limit is None else limit
    if requested_limit < 1:
        raise ValueError("search limit must be positive")
    effective_limit = min(
        requested_limit,
        budget.limits.max_search_results,
    )
    wanted_suffix = _normalize_suffix(suffix)
    needle = query.casefold()
    hits: list[SearchHit] = []
    skipped: list[str] = []
    truncated = False
    for path in boundary.iter_files():
        budget.consume(scanned_files=1)
        relative = boundary.relative_ref(path)
        if wanted_suffix is not None and path.suffix.casefold() != wanted_suffix:
            continue
        size = path.stat().st_size
        if size > budget.limits.max_single_file_bytes:
            skipped.append(relative)
            continue
        try:
            raw = _read_bounded_bytes(path, budget)
        except ArtifactOversizeError:
            skipped.append(relative)
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle not in line.casefold():
                continue
            if len(hits) >= effective_limit:
                truncated = True
                break
            hits.append(
                SearchHit(
                    relative_path=relative,
                    line_number=line_number,
                    snippet=line.strip()[: budget.limits.max_snippet_characters],
                )
            )
        if truncated:
            break
    return ArtifactSearchResult(
        query=query,
        hits=tuple(hits),
        skipped_oversize_files=tuple(skipped),
        truncated=truncated,
    )
