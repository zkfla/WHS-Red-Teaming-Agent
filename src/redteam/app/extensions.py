"""Selected extension collection and assembly metadata, scoped to one run."""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.metadata import entry_points

import pluggy

from redteam.core.extensions import (
    ENTRY_POINT_GROUP,
    Contribution,
    ExtensionDeclaration,
    ExtensionHooks,
)


class ExtensionAssemblyError(ValueError):
    pass


class ExtensionAssembly:
    def __init__(self, declarations: Iterable[ExtensionDeclaration] = ()) -> None:
        self.declarations = tuple(declarations)
        self._contributions: dict[tuple[str, str], Contribution] = {}
        self._owners: dict[tuple[str, str], str] = {}
        packages: set[str] = set()
        for declaration in self.declarations:
            if not declaration.extension_id.strip() or not declaration.version.strip():
                raise ExtensionAssemblyError("extension id and version must not be blank")
            if declaration.extension_id in packages:
                raise ExtensionAssemblyError(f"duplicate extension: {declaration.extension_id}")
            packages.add(declaration.extension_id)
            for contribution in declaration.contributions:
                if not all(
                    value.strip()
                    for value in (
                        contribution.kind,
                        contribution.contribution_id,
                        contribution.version,
                    )
                ):
                    raise ExtensionAssemblyError("contribution identity must not be blank")
                key = (contribution.kind, contribution.contribution_id)
                if key in self._contributions:
                    raise ExtensionAssemblyError(f"duplicate contribution: {key}")
                self._contributions[key] = contribution
                self._owners[key] = declaration.extension_id

    def validate_references(self, available: Iterable[tuple[str, str]] = ()) -> None:
        known = set(available) | self._contributions.keys()
        for key, contribution in self._contributions.items():
            for reference in contribution.requires:
                target = (reference.kind, reference.contribution_id)
                if not reference.optional and target not in known:
                    raise ExtensionAssemblyError(
                        f"{self._owners[key]}:{key} requires missing {target}"
                    )

    def contributions(self, kind: str) -> tuple[Contribution, ...]:
        return tuple(
            value for (category, _), value in self._contributions.items() if category == kind
        )

    def resolve(self, kind: str, contribution_id: str) -> Contribution:
        try:
            return self._contributions[(kind, contribution_id)]
        except KeyError:
            raise ExtensionAssemblyError(
                f"unknown contribution: {(kind, contribution_id)}"
            ) from None

    def binding(self) -> dict[str, str]:
        """Stable versions of declarations and contributions; no live objects."""
        return {
            **{f"extension:{item.extension_id}": item.version for item in self.declarations},
            **{
                f"{kind}:{identity}": json.dumps(
                    {
                        "owner": self._owners[(kind, identity)],
                        "version": item.version,
                        "requires": sorted(
                            (ref.kind, ref.contribution_id, ref.optional) for ref in item.requires
                        ),
                        "meaningful_change_keys": sorted(item.meaningful_change_keys),
                        "target_ids": sorted(item.target_ids),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for (kind, identity), item in self._contributions.items()
            },
        }

    def validate_kinds(self, supported: Iterable[str]) -> None:
        unsupported = {kind for kind, _ in self._contributions} - set(supported)
        if unsupported:
            raise ExtensionAssemblyError(f"unsupported contribution kinds: {sorted(unsupported)}")


def collect_extensions(
    selected: Iterable[str] = (), *, plugins: Iterable[object] = ()
) -> ExtensionAssembly:
    """Inspect metadata first and load only explicitly selected entry points."""
    manager = pluggy.PluginManager("redteam")
    manager.add_hookspecs(ExtensionHooks)
    discovered = entry_points(group=ENTRY_POINT_GROUP)
    selected_names = tuple(selected)
    if len(set(selected_names)) != len(selected_names):
        raise ExtensionAssemblyError("duplicate selected entry point")
    for name in selected_names:
        matches = [entry for entry in discovered if entry.name == name]
        if len(matches) != 1:
            raise ExtensionAssemblyError(f"extension entry point must be unique: {name}")
        manager.register(matches[0].load(), name=name)
    for index, plugin in enumerate(plugins):
        manager.register(plugin, name=f"explicit:{index}")
    manager.check_pending()
    declarations = [item for group in manager.hook.redteam_extensions() for item in group]
    return ExtensionAssembly(sorted(declarations, key=lambda item: item.extension_id))
