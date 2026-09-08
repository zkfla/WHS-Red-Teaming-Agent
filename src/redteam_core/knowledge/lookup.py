"""Operation-scoped Observation lookup. Never persisted or reused across commits."""

from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum

from redteam_core.investigation.requirements import Observation

MAX_OBSERVATION_INDEX_ENTRIES = 100_000
MAX_OBSERVATION_INDEX_REFERENCE_BYTES = 8_388_608


class ObservationLookupMode(StrEnum):
    FULL_SCAN = "full_scan"
    INDEXED = "indexed"


def index_observations(
    observations: Iterable[Observation],
    *,
    max_entries: int | None = None,
    max_reference_bytes: int | None = None,
) -> dict[str, list[Observation]] | None:
    """Return a complete admitted index, or None to request full-scan fallback.

    Bytes charge the UTF-8 observation/requirement IDs for each membership, not
    payloads or Python heap/RSS. Entry count independently bounds container growth.
    Overflow discards the private partial index; it never truncates canonical data.
    """
    max_entries = MAX_OBSERVATION_INDEX_ENTRIES if max_entries is None else max_entries
    max_reference_bytes = (
        MAX_OBSERVATION_INDEX_REFERENCE_BYTES
        if max_reference_bytes is None
        else max_reference_bytes
    )
    for value, ceiling in (
        (max_entries, MAX_OBSERVATION_INDEX_ENTRIES),
        (max_reference_bytes, MAX_OBSERVATION_INDEX_REFERENCE_BYTES),
    ):
        if type(value) is not int or not 0 <= value <= ceiling:
            raise ValueError("observation index cap must be a bounded nonnegative integer")
    grouped: dict[str, list[Observation]] = defaultdict(list)
    entries = reference_bytes = 0
    for observation in observations:
        size = len(observation.observation_id.encode("utf-8")) + len(
            observation.requirement_id.encode("utf-8")
        )
        if entries >= max_entries or reference_bytes + size > max_reference_bytes:
            return None
        grouped[observation.requirement_id].append(observation)
        entries += 1
        reference_bytes += size
    return dict(grouped)


def observations_for_requirement(
    observations: Iterable[Observation],
    requirement_id: str,
    index: dict[str, list[Observation]] | None,
) -> Iterable[Observation]:
    if index is not None:
        return index.get(requirement_id, ())
    return (row for row in observations if row.requirement_id == requirement_id)
