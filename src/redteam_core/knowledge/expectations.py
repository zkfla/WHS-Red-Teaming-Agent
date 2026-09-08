from __future__ import annotations

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession, ExpectationState
from redteam_core.models.security import ExpectationRef


def record_expectation(
    session: CoreSession,
    expectation: ExpectationRef,
    *,
    catalog: ExtensionCatalog,
) -> bool:
    """Record one typed normative expectation without changing its provenance authority."""

    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before recording an expectation")
    validated = ExpectationRef.model_validate(expectation.model_dump(mode="python"))
    registration = catalog.expectation_types.require(validated.expectation_type_id)
    registration.decode(validated.payload)

    candidate = session.expectations.model_copy(deep=True)
    existing = candidate.expectations.get(validated.expectation_id)
    if existing is not None:
        if existing != validated:
            raise ValueError("expectation ID collision with different content")
        return False
    candidate.expectations[validated.expectation_id] = validated
    candidate.revision += 1
    session.expectations = ExpectationState.model_validate(candidate.model_dump(mode="python"))
    return True
