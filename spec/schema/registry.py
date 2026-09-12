from __future__ import annotations

from pathlib import Path

import yaml

from spec.schema.tool_manifest import ToolManifest


class ToolRegistry:
    __slots__ = ("_by_id", "_source")

    def __init__(self) -> None:
        self._by_id: dict[str, ToolManifest] = {}
        self._source: dict[str, Path] = {}

    # ── 적재 ─────────────────────────────────────────────────────────
    @classmethod
    def load(cls, *roots: str | Path) -> tuple["ToolRegistry", list[str]]:
        reg, errs = cls(), []
        for f in sorted(p for r in roots for p in Path(r).glob("*.yaml")):
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                errs.append(f"{f.name}: YAML 파싱 실패 — {exc}")
                continue
            try:
                m = ToolManifest.model_validate(raw)
            except Exception as exc:
                lines = [l.strip() for l in str(exc).splitlines()]
                errs.append(f"{f.name}: 스키마 위반 — {' / '.join(lines[1:4]) or lines[0]}")
                continue
            errs += reg.add(m, f)
        return reg, errs

    def add(self, manifest: ToolManifest, source: Path | None = None) -> list[str]:
        if manifest.id in self._by_id:
            prev = self._source.get(manifest.id, Path("(알 수 없음)"))
            return [f"{manifest.id}: id 중복 — {prev} 와 {source or '(메모리)'} 가 "
                    f"같은 id 를 선언했다. 하나는 조용히 사라진다"]
        self._by_id[manifest.id] = manifest
        if source is not None:
            self._source[manifest.id] = source
        return []

    # ── 조회 ─────────────────────────────────────────────────────────
    def get(self, tool_id: str) -> ToolManifest | None:
        return self._by_id.get(tool_id)

    def require(self, tool_id: str) -> ToolManifest:
        m = self._by_id.get(tool_id)
        if m is None:
            raise KeyError(f"레지스트리에 없는 tool {tool_id!r} (있는 것: {sorted(self._by_id)})")
        return m

    def source_of(self, tool_id: str) -> Path | None:
        return self._source.get(tool_id)

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def all(self) -> list[ToolManifest]:
        return [self._by_id[i] for i in self.ids()]

    def as_dict(self) -> dict[str, ToolManifest]:
        return dict(self._by_id)

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self):
        return iter(self.all())

    # ── MCP 노출 ─────────────────────────────────────────────────────
    def mcp_tools(self, available: set[str] | None = None) -> list[dict]:
        out = []
        for m in self.all():
            if not m.mcp.expose or m.deprecated:
                continue
            if available is not None and m.mcp.hide_when_unavailable and m.id not in available:
                continue
            out.append(m.to_mcp_tool())
        return out
