from redteam_core.extension import (
    BundleKind,
    BundleManifest,
    DomainBundle,
    RelationTypeRegistration,
)
from redteam_core.graph.semantics import RelationSemantics
from tests.reference_domains.common import fp

BUNDLE_ID = "refintegration"
RELATION_TYPE = "refintegration.relation.cross_domain@1"


def bundle() -> DomainBundle:
    return DomainBundle(
        manifest=BundleManifest(
            bundle_id=BUNDLE_ID,
            namespace=BUNDLE_ID,
            version="1.0.0",
            bundle_kind=BundleKind.INTEGRATION,
            implementation_fingerprint=fp("e"),
            requires_bundle_ids=("refartifact", "refrequest"),
        ),
        relation_registrations=(
            RelationTypeRegistration(
                type_id=RELATION_TYPE,
                schema_version="1",
                owner_bundle_id=BUNDLE_ID,
                implementation_fingerprint=fp("f"),
                semantics=RelationSemantics(traversable=True, context_bridge=True),
            ),
        ),
    )
