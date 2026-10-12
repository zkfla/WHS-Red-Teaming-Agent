"""Engagement files, one per investigation.

A directory holds ``<id>.yaml`` files; ids are fixed-shape names, never paths, so a
request cannot address a file outside the directory. An engagement file given on the
command line is pinned under its own id even when it lives elsewhere. Nothing is
active until it is selected explicitly: scope is authorization.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from redteam.app.cli.display import safe_text
from redteam.app.web.engagement import EngagementError, EngagementStore

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LIST_LIMIT = 100
_SLUG = re.compile(r"[^a-z0-9]+")


class EngagementCatalog:
    def __init__(self, directory: Path, pinned: Iterable[Path] = ()) -> None:
        self.directory = directory
        self._pinned: dict[str, Path] = {}
        for path in pinned:
            self._pinned[self._free_id(path.stem, taken=set(self._pinned))] = path

    def _free_id(self, text: str, *, taken: set[str]) -> str:
        base = _SLUG.sub("-", text.lower()).strip("-")[:48] or "engagement"
        candidate, number = base, 1
        while candidate in taken or (self.directory / f"{candidate}.yaml").exists():
            number += 1
            candidate = f"{base}-{number}"
        return candidate

    def pinned_ids(self) -> list[str]:
        return list(self._pinned)

    def new_id(self, name: str | None) -> str:
        return self._free_id(name or "", taken=set(self._pinned))

    def path(self, engagement_id: object) -> Path:
        if not isinstance(engagement_id, str) or not ID_PATTERN.match(engagement_id):
            raise EngagementError("unknown_engagement")
        return self._pinned.get(engagement_id) or self.directory / f"{engagement_id}.yaml"

    def store(self, engagement_id: object, *, must_exist: bool = True) -> EngagementStore:
        store = EngagementStore(self.path(engagement_id))
        if must_exist and not store.exists():
            raise EngagementError("unknown_engagement")
        return store

    def entries(self) -> list[dict[str, Any]]:
        found: dict[str, Path] = dict(self._pinned)
        if self.directory.is_dir():
            files = sorted(
                (p for p in self.directory.glob("*.yaml") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in files:
                if ID_PATTERN.match(path.stem) and path.stem not in found:
                    found[path.stem] = path
        items = []
        for engagement_id, path in list(found.items())[:_LIST_LIMIT]:
            status = EngagementStore(path).status()
            summary = status["summary"]
            hosts = summary["scope"]["allowed_hosts"][:3] if summary else []
            items.append(
                {
                    "id": engagement_id,
                    "name": summary["name"] if summary else safe_text(path.stem, limit=64),
                    "valid": status["valid"],
                    "editable": status["editable"],
                    "hosts": hosts,
                }
            )
        return items
