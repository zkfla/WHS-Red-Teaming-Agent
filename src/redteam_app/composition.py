from __future__ import annotations

from redteam_core.extension import DomainBundle, ExtensionCatalog, compose_bundles


def build_runtime_catalog(*, bundles: tuple[DomainBundle, ...]) -> ExtensionCatalog:
    """Explicit composition root.

    Plugin auto-discovery is intentionally not part of Batch A. Callers supply trusted bundles
    explicitly; composition validates dependencies, schemas and collisions, then freezes semantics.
    """

    return compose_bundles(bundles)
