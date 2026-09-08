"""Deterministic advisory ranking. No dispatch, reservation publication or truth writes."""

from datetime import datetime
from math import ceil
from typing import Protocol

from redteam_core.investigation.coverage import TERMINAL_DISPOSITIONS, score_security_candidate
from redteam_core.investigation.resolution import ResolverRuntimeRegistry
from redteam_core.knowledge.commit import _is_fresh, _require_aware
from redteam_core.knowledge.lookup import index_observations, observations_for_requirement
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, sha256_text, stable_id
from redteam_core.models.operational_budget import RequestReservation, ReservationStatus
from redteam_core.models.priority import (
    PriorityAssessment,
    PriorityComponent,
    PriorityFacts,
    PriorityProviderDescriptor,
    PriorityScore,
    PrioritySelection,
    PriorityState,
    PriorityTerminalBinding,
    RootPriorityState,
)
from redteam_core.models.resolution_receipt import (
    MAX_RESOLUTION_RECEIPT_LEDGER_BYTES,
    MAX_RESOLUTION_RECEIPTS,
)
from redteam_core.models.runtime import RuntimeBudgetLimits, RuntimeState
from redteam_core.runtime.operational_budget import OperationalBudgetExceeded, reserve_request


class PriorityProvider(Protocol):
    descriptor: PriorityProviderDescriptor

    def score(self, facts: PriorityFacts) -> PriorityScore: ...


class BaselinePriorityProvider:
    descriptor = PriorityProviderDescriptor(
        provider_id="core.priority.baseline@1", implementation_version="1", floor_slots=0
    )

    def score(self, facts: PriorityFacts) -> PriorityScore:
        return PriorityScore(
            candidate_id=facts.candidate_id,
            components=(PriorityComponent(name="legacy_score", value=facts.baseline_total),),
            reason="Existing deterministic candidate score; no learned probability.",
        )


class ExpectedUtilityPriorityProvider:
    def __init__(self, *, floor_slots: int = 1, max_wait_batches: int = 256):
        self.descriptor = PriorityProviderDescriptor(
            provider_id="core.priority.expected-utility@1",
            implementation_version="1",
            floor_slots=floor_slots,
            max_wait_batches=max_wait_batches,
        )
        if floor_slots < 1:
            raise ValueError("expected utility requires a positive fairness floor")

    def score(self, facts: PriorityFacts) -> PriorityScore:
        values = {
            "resolvable": min(facts.resolvable_count, 64) * 12,
            "shared_unresolved_roots": min(facts.shared_unresolved_roots, 256) * 4,
            "fresh_evidence": min(facts.fresh_count, 64) * 8,
            "conflict": min(facts.conflict_count, 64) * 8,
            "cost": -facts.cost_bucket * 4,
            "repeated_failures": -min(facts.failure_count, 256) * 5,
            "aging": min(facts.age_batches, 256),
        }
        return PriorityScore(
            candidate_id=facts.candidate_id,
            components=tuple(
                PriorityComponent(name=name, value=value) for name, value in values.items()
            ),
            reason="Deterministic cost/opportunity buckets; FALSE and conflict are useful.",
        )


def _hash(value) -> str:
    return "sha256:" + sha256_text(canonical_json(value))


def _root_dependencies(session, candidates):
    requirements = {key for row in candidates for key in row.requirement_ids}
    relations = {
        key
        for row in candidates
        for key in (
            *row.supporting_relation_ids,
            *row.contradicting_relation_ids,
            *row.guard_relation_ids,
        )
    }
    seen_requirements, seen_relations = set(), set()
    while requirements != seen_requirements or relations != seen_relations:
        if len(requirements) + len(relations) > 4096:
            raise ValueError("priority dependency closure exceeds bounded capacity")
        for key in sorted(requirements - seen_requirements):
            seen_requirements.add(key)
            req = session.investigation.requirements.get(key)
            if req is not None:
                relations.update(req.basis_relation_ids)
        for key in sorted(relations - seen_relations):
            seen_relations.add(key)
            relation = session.semantic.relations.get(key)
            if relation is not None:
                requirements.update(relation.requirement_ids)
    return requirements, relations


def root_evidence_fingerprint(
    session: CoreSession, candidates, *, now: datetime, observation_index=None
) -> str:
    """Only this root's dependencies, including missing refs and freshness transitions."""
    _require_aware(now)
    requirements, relations = _root_dependencies(session, candidates)
    expectations = {key for row in candidates for key in row.expectation_ids}
    expectations.update(
        ref for row in candidates for ref in row.grounding_refs if ref.startswith("exp_")
    )
    observations = set()
    rows = (
        (row for key in sorted(requirements) for row in observation_index.get(key, ()))
        if observation_index is not None
        else session.investigation.observations.values()
    )
    for row in rows:
        if row.requirement_id not in requirements:
            continue
        req = session.investigation.requirements.get(row.requirement_id)
        observations.add(
            canonical_json(
                {
                    "requirement": row.requirement_id,
                    "payload": row.payload.content_hash,
                    "resolver": row.resolver_id,
                    "trust": row.provenance.trust_class.value,
                    "fresh": _is_fresh(req, row, now) if req else False,
                    "snapshot": row.validity.snapshot_ref,
                    "evidence": sorted(row.evidence_ids),
                }
            )
        )
    return _hash(
        {
            "candidates": sorted(
                {
                    canonical_json(
                        {
                            "type": row.candidate_type_id,
                            "claim_contract": row.claim_contract.model_dump(mode="json"),
                            "claim_ceiling": row.max_claim_status.value,
                            "claim": row.current_claim_status.value,
                            "support": sorted(row.supporting_relation_ids),
                            "contradiction": sorted(row.contradicting_relation_ids),
                            "guards": sorted(row.guard_relation_ids),
                            "requirements": sorted(row.requirement_ids),
                            "expectations": sorted(row.expectation_ids),
                            "evidence": sorted(row.evidence_ids),
                            "grounding": sorted(row.grounding_refs),
                            "payload": row.candidate_payload.content_hash
                            if row.candidate_payload
                            else None,
                        }
                    )
                    for row in candidates
                }
            ),
            "relations": {
                key: session.semantic.relations[key].model_dump(mode="json")
                if key in session.semantic.relations
                else None
                for key in sorted(relations)
            },
            "requirements": {
                key: session.investigation.requirements[key].model_dump(mode="json")
                if key in session.investigation.requirements
                else None
                for key in sorted(requirements)
            },
            "expectations": {
                key: {
                    "hash": session.expectations.expectations[key].payload.content_hash,
                    "trust": session.expectations.expectations[key].provenance.trust_class.value,
                    "evidence": sorted(session.expectations.expectations[key].evidence_ids),
                }
                if key in session.expectations.expectations
                else None
                for key in sorted(expectations)
            },
            "observations": sorted(observations),
        }
    )


def _request_feasible(runtime, limits, descriptor, requirement) -> bool:
    ledger = runtime.operational_budget
    if ledger is None:
        return False
    pending = tuple(
        row
        for row in ledger.reservations
        if row.status in {ReservationStatus.RESERVED, ReservationStatus.DISPATCHED}
    )
    if len(runtime.resolution_receipts) + len(pending) >= MAX_RESOLUTION_RECEIPTS:
        return False
    if (
        sum(row.serialized_bytes for row in runtime.resolution_receipts)
        + sum(row.reserved_receipt_bytes for row in pending)
        + descriptor.max_receipt_bytes_per_call
        > MAX_RESOLUTION_RECEIPT_LEDGER_BYTES
    ):
        return False
    if (
        len(requirement.model_dump_json().encode("utf-8")) + 2048
        > descriptor.max_receipt_bytes_per_call
    ):
        return False
    if (
        sum(
            row.resolver_id == descriptor.resolver_id
            and row.status is not ReservationStatus.RELEASED
            for row in ledger.reservations
        )
        >= descriptor.max_calls
    ):
        return False
    try:
        projected = reserve_request(
            ledger,
            RequestReservation(
                attempt_id=f"priority-preview:{ledger.revision}:{descriptor.resolver_id}",
                provider_id=descriptor.provider_id,
                resolver_id=descriptor.resolver_id,
                reserved_tokens=descriptor.max_tokens_per_call or 0,
                token_bound_guaranteed=descriptor.max_tokens_per_call is not None,
                reserved_receipt_bytes=descriptor.max_receipt_bytes_per_call,
            ),
            expected_revision=ledger.revision,
        )
    except OperationalBudgetExceeded:
        return False
    if (
        limits.max_requests is not None
        and (
            runtime.usage.requests
            + projected.charged_requests
            - runtime.operational_accounted_requests
        )
        > limits.max_requests
    ):
        return False
    if limits.max_tokens is not None:
        if any(
            not row.token_bound_guaranteed and row.status is not ReservationStatus.RELEASED
            for row in projected.reservations
        ):
            return False
        if (
            runtime.usage.tokens + projected.charged_tokens - runtime.operational_accounted_tokens
        ) > limits.max_tokens:
            return False
    return True


def prepare_priority_selection(
    session: CoreSession,
    runtime: RuntimeState,
    *,
    registry: ResolverRuntimeRegistry,
    provider: PriorityProvider,
    permissions: frozenset[str],
    now: datetime,
    limits: RuntimeBudgetLimits,
    limit: int = 6,
) -> tuple[PrioritySelection, PriorityState]:
    """Return privately prepared state; a durable caller must recheck and publish it."""
    _require_aware(now)
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("priority limit must be an integer in 1..64")
    if not registry.frozen:
        raise ValueError("priority requires frozen resolver descriptors")
    descriptor = PriorityProviderDescriptor.model_validate(
        provider.descriptor.model_dump(mode="python")
    )
    state = PriorityState.model_validate(runtime.priority.model_dump(mode="python"))
    if len(session.candidate_state.candidates) > descriptor.max_candidates:
        raise ValueError("priority candidate capacity exceeded; no silent truncation")
    if len(state.selections) >= 256:
        raise ValueError("priority selection history capacity exhausted")
    groups = {}
    for row in session.candidate_state.candidates.values():
        groups.setdefault(row.root_cause.qualified_key, []).append(row)
    if len(groups) > descriptor.max_roots:
        raise ValueError("priority root capacity exceeded; no silent truncation")
    # One operation-local dependency inventory, never authoritative state mutation.
    shared = {}
    for root, group in groups.items():
        for req in _root_dependencies(session, group)[0]:
            shared.setdefault(req, set()).add(root)
    observations = index_observations(session.investigation.observations.values())
    roots = {key: row.model_copy(update={"active": False}) for key, row in state.roots.items()}
    assessments = []
    for root, group in sorted(groups.items()):
        old = roots.get(root)
        evidence_hash = root_evidence_fingerprint(
            session, group, now=now, observation_index=observations
        )
        req_ids = _root_dependencies(session, group)[0]
        physical_failures = [
            row
            for row in runtime.resolution_receipts
            if row.failure is not None and row.requirement.requirement_id in req_ids
        ]
        backed_failure_ids = {row.failure.failure_id for row in physical_failures}
        failures = {
            row.failure_id
            for row in session.investigation.resolution_failures.values()
            if row.requirement_id in req_ids and row.failure_id not in backed_failure_ids
        }
        failures.update(
            stable_id("fail", "physical-priority-attempt", row.attempt_id)
            for row in physical_failures
        )
        failures.update(old.seen_failure_ids if old else ())
        terminal = old.terminal if old else None
        if terminal is None and all(row.disposition in TERMINAL_DISPOSITIONS for row in group):
            # Unknown historical binding stays unknown; never bind it to today's evidence.
            disposition = sorted(
                group, key=lambda row: (row.disposition.value != "MERGED", row.candidate_id)
            )[0].disposition
            terminal = PriorityTerminalBinding(disposition=disposition)
        reevaluate = terminal is not None and terminal.evidence_hash != evidence_hash
        roots[root] = RootPriorityState(
            root_key=root,
            evidence_hash=evidence_hash,
            active=True,
            terminal=terminal,
            reevaluation_required=reevaluate,
            seen_failure_ids=tuple(sorted(failures)),
            first_seen_batch=old.first_seen_batch if old else state.batch_sequence,
            last_selected_batch=old.last_selected_batch if old else 0,
            selection_count=old.selection_count if old else 0,
        )
        ranked = []
        for candidate in group:
            fresh = conflicts = resolvable = shared_count = 0
            costs, blocked = [], []
            candidate_requirements = _root_dependencies(session, (candidate,))[0]
            for key in sorted(candidate_requirements):
                req = session.investigation.requirements.get(key)
                if req is None:
                    blocked.append("MISSING_REQUIREMENT")
                    continue
                hashes = {
                    row.payload.content_hash
                    for row in observations_for_requirement(
                        session.investigation.observations.values(), key, observations,
                    )
                    if row.provenance.trust_class.authoritative and _is_fresh(req, row, now)
                }
                if hashes:
                    fresh += 1
                    conflicts += len(hashes) > 1
                    continue
                shared_count += len(shared[key]) - 1
                eligible = [
                    resolver.descriptor
                    for resolver in registry.eligible(req, available_permissions=permissions)
                    if resolver.descriptor.side_effect_free
                ]
                feasible = [
                    item for item in eligible if _request_feasible(runtime, limits, item, req)
                ]
                if feasible:
                    resolvable += 1
                    costs.append(min(item.cost_rank for item in feasible) // 20)
                else:
                    blocked.append(
                        "BUDGET_UNAVAILABLE" if eligible else "NO_PERMITTED_PASSIVE_RESOLVER"
                    )
            is_eligible = not candidate_requirements or fresh > 0 or resolvable > 0
            if terminal is not None and (terminal.locked or not reevaluate):
                is_eligible = False
                blocked.append("LOCKED_TERMINAL" if terminal.locked else "TERMINAL_CURRENT")
            facts = PriorityFacts(
                candidate_id=candidate.candidate_id,
                root_key=root,
                family=f"{candidate.root_cause.owner_bundle_id}:{candidate.root_cause.family_id}",
                baseline_total=score_security_candidate(candidate).total,
                resolvable_count=resolvable,
                shared_unresolved_roots=shared_count,
                fresh_count=fresh,
                conflict_count=conflicts,
                cost_bucket=min(costs, default=0),
                failure_count=len(failures),
                age_batches=state.batch_sequence
                - (old.last_selected_batch if old else state.batch_sequence),
            )
            score = PriorityScore.model_validate(
                provider.score(facts.model_copy(deep=True)).model_dump(mode="python")
            )
            ranked.append(
                PriorityAssessment(
                    facts=facts,
                    score=score,
                    evidence_hash=evidence_hash,
                    eligible=is_eligible,
                    blocked_reasons=tuple(sorted(set(blocked))),
                    reevaluation_required=reevaluate,
                )
            )
        assessments.append(
            sorted(
                ranked, key=lambda row: (not row.eligible, -row.score.total, row.facts.candidate_id)
            )[0]
        )
    eligible = sorted(
        (row for row in assessments if row.eligible),
        key=lambda row: (-row.score.total, row.facts.candidate_id),
    )
    floor = min(limit, descriptor.floor_slots)
    bound = ceil(len(eligible) / floor) if floor and eligible else None
    if bound is not None and bound > descriptor.max_wait_batches:
        raise ValueError("infeasible fairness bound for the eligible root pool and floor slots")
    chosen = sorted(
        eligible,
        key=lambda row: (roots[row.facts.root_key].last_selected_batch, row.facts.root_key),
    )[:floor]
    chosen_ids = {row.facts.candidate_id for row in chosen}
    families = {row.facts.family for row in chosen}
    for diverse in (True, False):
        for row in eligible:
            if len(chosen) >= limit:
                break
            if row.facts.candidate_id in chosen_ids or (diverse and row.facts.family in families):
                continue
            chosen.append(row)
            chosen_ids.add(row.facts.candidate_id)
            families.add(row.facts.family)
    batch = state.batch_sequence + 1
    selection = PrioritySelection(
        batch_index=batch,
        provider_binding=descriptor.binding_id,
        input_hash=priority_input_fingerprint(session, runtime, registry, permissions, now, limits),
        expected_priority_revision=state.revision,
        assessments=tuple(assessments),
        selected_candidate_ids=tuple(row.facts.candidate_id for row in chosen),
        fairness_bound_batches=bound,
    )
    for row in chosen:
        current = roots[row.facts.root_key]
        roots[row.facts.root_key] = current.model_copy(
            update={
                "selection_count": current.selection_count + 1,
                "last_selected_batch": batch,
            }
        )
    return selection, PriorityState(
        revision=state.revision + 1,
        batch_sequence=batch,
        roots=roots,
        selections=(*state.selections, selection),
        decisions=state.decisions,
    )


def priority_input_fingerprint(session, runtime, registry, permissions, now, limits) -> str:
    return _hash(
        {
            "session": session.model_dump(mode="json"),
            "runtime": runtime.model_dump(mode="json"),
            "descriptors": [row.model_dump(mode="json") for row in registry.descriptors()],
            "calls": {
                row.resolver_id: registry.call_count(row.resolver_id)
                for row in registry.descriptors()
            },
            "permissions": sorted(permissions),
            "freshness": {
                key: _is_fresh(session.investigation.requirements[row.requirement_id], row, now)
                for key, row in session.investigation.observations.items()
                if row.requirement_id in session.investigation.requirements
            },
            "limits": limits.model_dump(mode="json"),
        }
    )


def stale_priority_decision_roots(session: CoreSession, priority: PriorityState, *, now: datetime):
    """Recheck at finish too; a prior scheduling snapshot cannot prove current completeness."""
    if not priority.selections:
        return ()
    groups = {}
    for candidate in session.candidate_state.candidates.values():
        groups.setdefault(candidate.root_cause.qualified_key, []).append(candidate)
    stale = []
    for key, group in sorted(groups.items()):
        if not all(row.disposition in TERMINAL_DISPOSITIONS for row in group):
            continue
        root = priority.roots.get(key)
        current = root_evidence_fingerprint(session, group, now=now)
        if root is None or root.terminal is None or root.terminal.evidence_hash != current:
            stale.append(key)
    return tuple(stale)
