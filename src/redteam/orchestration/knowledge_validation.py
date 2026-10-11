"""Internal assessment material for execution checks (Agent team, A1).

Execution checks read this; the model only ever sees the capped
projection built by the knowledge projection path. Display caps
(provided/fact/observation/evidence-id limits) live in the projection
only and never shrink this material.

Per checked run this holds: every known target with its origin
(declared vs observed) and confirmation state; every grounded fact ref,
where an evidence ref exists only for a same-run record with a raw
payload behind a real measured write; registered principal/session
wiring as refs only, never secrets; lookup-failure flags that tell a
failed read apart from an empty result; and other-run spillover kept
out of the refs.

Leaf module: no imports from the knowledge collector or the execution
engine. The collector builds the material from the existing
claim-level grounding; the engine only reads it via the helpers below.
Never stored in checkpoints, events, or model payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AssessmentError(ValueError):
    """A code-owned rule violation against the internal material."""


_SESSION_ARGUMENT_FIELDS = ("session_ref", "first_session_ref", "second_session_ref")


@dataclass
class InternalTarget:
    """One known target with its provenance summary (refs only)."""

    node_id: str
    node_type: str = ""
    origin: str = "observed"
    verification: str = "unconfirmed"
    address: str = ""
    target_ref: str | None = None
    # Present only for a verified reference to an unvisited address.
    execution_target: Any = None


@dataclass
class InternalAssessment:
    """Full pre-truncation refs for one execution check."""

    run_id: str = ""
    targets: dict[str, InternalTarget] = field(default_factory=dict)
    fact_refs: set[str] = field(default_factory=set)
    principal_bindings: dict[str, set[str]] = field(default_factory=dict)
    graph_lookup_failed: bool = False
    graph_error: str | None = None
    evidence_lookup_failed: bool = False
    evidence_error: str | None = None
    historical_targets: list[dict[str, Any]] = field(default_factory=list)
    historical_fact_count: int = 0
    fact_fingerprint: str = ""


def assessment_blocked(material: InternalAssessment) -> str | None:
    """Why this material admits no execution candidate, if anything.

    A failed store read is never an empty list: checks stop and the
    caller routes to the existing plan-failure diagnosis.
    """
    if material.graph_lookup_failed:
        return "graph lookup failed; execution checks need a fresh read, not an empty list"
    if material.evidence_lookup_failed:
        return "evidence lookup failed; measured claims stay ungrounded for this check"
    return None


def known_target_ids(material: InternalAssessment) -> set[str]:
    """Every usable target id: declared scope plus observed nodes."""
    return set(material.targets)


def target_record(material: InternalAssessment, node_id: str) -> InternalTarget | None:
    return material.targets.get(str(node_id))


def has_fact_ref(material: InternalAssessment, ref: str) -> bool:
    return str(ref) in material.fact_refs


def is_evidence_grounded(material: InternalAssessment, evidence_id: str) -> bool:
    return "evidence:" + str(evidence_id) in material.fact_refs


def known_principals(material: InternalAssessment) -> set[str]:
    return set(material.principal_bindings)


def bindings_for(material: InternalAssessment) -> dict[str, set[str]]:
    return {key: set(value) for key, value in material.principal_bindings.items()}


def _text(value: object) -> str:
    return str(getattr(value, "value", value))


def check_ready_step(path: object, step: object, material: InternalAssessment) -> list[str]:
    """Step gates against the internal material (same rules, full refs)."""
    problems: list[str] = []
    steps = {item.step_id: item for item in getattr(path, "steps", [])}
    if _text(getattr(step, "status", "")) not in {"pending", "running"}:
        problems.append("step is not open for execution")
    for dependency in getattr(step, "depends_on", []) or []:
        parent = steps.get(dependency)
        if parent is None or _text(getattr(parent, "status", "")) != "completed":
            problems.append(f"prerequisite step not completed: {dependency}")
    for reference in getattr(step, "required_fact_refs", []) or []:
        if str(reference) not in material.fact_refs:
            problems.append(f"unconfirmed required fact: {reference}")
    return problems


def check_principal_arguments(
    step: object,
    principal: str | None,
    arguments: dict[str, object],
    material: InternalAssessment,
) -> None:
    """Principal/session wiring check against the internal material."""
    bindings = material.principal_bindings
    session_refs = [
        str(arguments[name]) for name in _SESSION_ARGUMENT_FIELDS if arguments.get(name) is not None
    ]
    if principal and principal not in bindings:
        raise AssessmentError("action references an unregistered principal")
    if session_refs:
        if principal is None or principal == "anonymous":
            raise AssessmentError("session arguments require an identified principal")
        # Draft subjects describe an intended result, not account permissions.
        # Actual registrations and the selected identity bind the execution.
        allowed = {session for values in bindings.values() for session in values}
        if set(session_refs) - allowed or not set(session_refs) & bindings.get(principal, set()):
            raise AssessmentError("session arguments do not match accepted principal bindings")
    if arguments.get("principal_ref") is not None and str(arguments["principal_ref"]) != principal:
        raise AssessmentError("principal tool argument differs from action identity")


def registered_principal_for_arguments(
    arguments: dict[str, Any], material: InternalAssessment,
) -> str | None:
    """Bind the caller's selected session to a registered identity reference.

    This supplies execution bookkeeping, not permission. The existing
    principal check still validates every selected session at admission.
    Ambiguous registrations remain unresolved rather than choosing an owner.
    """
    explicit = arguments.get("principal_ref")
    if explicit is not None:
        return str(explicit)
    selected = next(
        (
            str(arguments[name]) for name in _SESSION_ARGUMENT_FIELDS
            if arguments.get(name) is not None
        ),
        None,
    )
    if selected is None:
        return None
    # The collector registers an explicit session reference as its own
    # accepted identity alias. Prefer that supplied alias over inferred
    # ownership among principal/credential nodes for the same session.
    if selected in material.principal_bindings.get(selected, set()):
        return selected
    owners = [
        principal for principal, sessions in material.principal_bindings.items()
        if principal != "anonymous" and selected in sessions
    ]
    return owners[0] if len(owners) == 1 else None


__all__ = [
    "AssessmentError",
    "InternalAssessment",
    "InternalTarget",
    "KnowledgeValidationSnapshot",
    "assessment_blocked",
    "bindings_for",
    "check_principal_arguments",
    "check_ready_step",
    "has_fact_ref",
    "is_evidence_grounded",
    "known_principals",
    "known_target_ids",
    "registered_principal_for_arguments",
    "target_record",
]


# Alias for the frozen collection API: the per-check material engine
# checks read. The name states the role; the shape stays identical.
KnowledgeValidationSnapshot = InternalAssessment
