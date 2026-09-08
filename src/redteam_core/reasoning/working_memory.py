from __future__ import annotations

from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, stable_id
from redteam_core.models.reasoning import (
    AgentWorkingMemory,
    HypothesisNextCheck,
    HypothesisReconciliationResult,
    WorkingHypothesis,
    WorkingHypothesisStatus,
    WorkingHypothesisUpsert,
    WorkingMemoryPatch,
    WorkingMemoryUpdateResult,
)
from redteam_core.models.security import CandidateDisposition, ClaimStatus, SecurityCandidate
from redteam_core.security import redact_free_text

MAX_WORKING_MEMORY_CHARACTERS = 12_000
MAX_WORKING_HYPOTHESES = 8
MAX_HYPOTHESIS_EVIDENCE_IDS = 24
MAX_HYPOTHESIS_MISSING_REASON_IDS = 16
MAX_HYPOTHESIS_REQUIREMENT_IDS = 16
MAX_HYPOTHESIS_GROUNDING_REFS = 32


class WorkingMemoryLimitExceeded(RuntimeError):
    pass


def _ordered_unique(items: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _canonical_grounding(
    session: CoreSession,
    *,
    candidate: SecurityCandidate | None,
    supporting_relation_ids: tuple[str, ...],
    contradicting_relation_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    relation_ids = (*supporting_relation_ids, *contradicting_relation_ids)
    relation_by_id = session.semantic.relations
    unknown = sorted(set(relation_ids) - set(relation_by_id))
    if unknown:
        raise ValueError(f"unknown relation IDs: {unknown}")

    evidence_ids: list[str] = []
    missing_reason_ids: list[str] = []
    requirement_ids: list[str] = []
    grounding_refs: list[str] = []
    if candidate is not None:
        evidence_ids.extend(candidate.evidence_ids)
        requirement_ids.extend(candidate.requirement_ids)
        grounding_refs.extend(candidate.grounding_refs)
    for relation_id in relation_ids:
        relation = relation_by_id[relation_id]
        evidence_ids.extend(relation.evidence_ids)
        missing_reason_ids.extend(relation.missing_reason_ids)
        requirement_ids.extend(relation.requirement_ids)
    return (
        _ordered_unique(evidence_ids)[:MAX_HYPOTHESIS_EVIDENCE_IDS],
        _ordered_unique(missing_reason_ids)[:MAX_HYPOTHESIS_MISSING_REASON_IDS],
        _ordered_unique(requirement_ids)[:MAX_HYPOTHESIS_REQUIREMENT_IDS],
        _ordered_unique(grounding_refs)[:MAX_HYPOTHESIS_GROUNDING_REFS],
    )


def _validate_candidate_binding(
    candidate: SecurityCandidate | None,
    *,
    supporting_relation_ids: tuple[str, ...],
    contradicting_relation_ids: tuple[str, ...],
) -> None:
    if candidate is None:
        return
    allowed = set(candidate.supporting_relation_ids) | set(candidate.contradicting_relation_ids)
    outside = set((*supporting_relation_ids, *contradicting_relation_ids)) - allowed
    if outside:
        raise ValueError(
            "candidate-bound hypothesis references unrelated relations: "
            f"{sorted(outside)}"
        )


def _status_grounding_errors(
    session: CoreSession,
    hypothesis: WorkingHypothesis,
    candidate: SecurityCandidate | None,
) -> list[str]:
    errors: list[str] = []
    relations = session.semantic.relations
    false_support = [
        relation_id
        for relation_id in hypothesis.supporting_relation_ids
        if relations[relation_id].verdict.value == "FALSE"
    ]
    true_contradictions = [
        relation_id
        for relation_id in hypothesis.contradicting_relation_ids
        if relations[relation_id].verdict.value == "TRUE"
    ]
    if false_support:
        errors.append(f"FALSE relations cannot support a hypothesis: {sorted(false_support)}")
    if hypothesis.status is WorkingHypothesisStatus.READY_FOR_PROPOSAL:
        candidate_ready = bool(
            candidate is not None
            and candidate.current_claim_status in {ClaimStatus.SUPPORTED, ClaimStatus.CONFIRMED}
            and candidate.disposition
            not in {
                CandidateDisposition.REJECTED,
                CandidateDisposition.MERGED,
                CandidateDisposition.BLOCKED_EXTERNAL,
            }
        )
        relation_ready = bool(
            hypothesis.supporting_relation_ids
            and hypothesis.evidence_ids
            and all(
                relations[item].verdict.value == "TRUE"
                for item in hypothesis.supporting_relation_ids
            )
            and not true_contradictions
        )
        if not (candidate_ready or relation_ready):
            errors.append("READY_FOR_PROPOSAL requires canonical supported grounding")
    return errors


def _build_hypothesis(
    session: CoreSession,
    existing: WorkingHypothesis | None,
    upsert: WorkingHypothesisUpsert,
    *,
    next_revision: int,
) -> WorkingHypothesis:
    cleaned_claim = redact_free_text(upsert.claim) if upsert.claim is not None else None
    cleaned_summary = (
        redact_free_text(upsert.decision_summary)
        if upsert.decision_summary is not None
        else None
    )
    cleaned_next_check = upsert.next_check
    if cleaned_next_check is not None:
        cleaned_next_check = cleaned_next_check.model_copy(
            update={"objective": redact_free_text(cleaned_next_check.objective)}
        )
    if existing is None:
        if not upsert.semantic_key or not cleaned_claim or not cleaned_summary:
            raise ValueError("new hypothesis requires semantic_key, claim, and decision_summary")
        hypothesis_id = stable_id("hyp", upsert.semantic_key)
        candidate_id = upsert.candidate_id
        candidate = (
            session.candidate_state.candidates.get(candidate_id)
            if candidate_id is not None
            else None
        )
        if candidate_id is not None and candidate is None:
            raise ValueError(f"unknown candidate ID: {candidate_id}")
        support = (
            upsert.supporting_relation_ids
            if upsert.supporting_relation_ids is not None
            else tuple(candidate.supporting_relation_ids)
            if candidate is not None
            else ()
        )
        contradictions = (
            upsert.contradicting_relation_ids
            if upsert.contradicting_relation_ids is not None
            else tuple(candidate.contradicting_relation_ids)
            if candidate is not None
            else ()
        )
        _validate_candidate_binding(
            candidate,
            supporting_relation_ids=support,
            contradicting_relation_ids=contradictions,
        )
        evidence, missing, requirements, grounding = _canonical_grounding(
            session,
            candidate=candidate,
            supporting_relation_ids=support,
            contradicting_relation_ids=contradictions,
        )
        return WorkingHypothesis(
            hypothesis_id=hypothesis_id,
            semantic_key=upsert.semantic_key,
            candidate_id=candidate_id,
            claim=cleaned_claim,
            status=upsert.status or WorkingHypothesisStatus.ACTIVE,
            supporting_relation_ids=support,
            contradicting_relation_ids=contradictions,
            evidence_ids=evidence,
            missing_reason_ids=missing,
            requirement_ids=requirements,
            grounding_refs=grounding,
            decision_summary=cleaned_summary,
            next_check=None if upsert.clear_next_check else cleaned_next_check,
            created_revision=next_revision,
            updated_revision=next_revision,
        )

    if upsert.semantic_key is not None and upsert.semantic_key != existing.semantic_key:
        raise ValueError("hypothesis semantic_key is immutable")
    if cleaned_claim is not None and " ".join(cleaned_claim.split()).casefold() != " ".join(
        existing.claim.split()
    ).casefold():
        raise ValueError("hypothesis claim is immutable")
    if upsert.candidate_id is not None and upsert.candidate_id != existing.candidate_id:
        raise ValueError("hypothesis candidate binding is immutable")

    candidate = (
        session.candidate_state.candidates.get(existing.candidate_id)
        if existing.candidate_id is not None
        else None
    )
    if existing.candidate_id is not None and candidate is None:
        raise ValueError(f"unknown candidate ID: {existing.candidate_id}")
    support = (
        upsert.supporting_relation_ids
        if upsert.supporting_relation_ids is not None
        else existing.supporting_relation_ids
    )
    contradictions = (
        upsert.contradicting_relation_ids
        if upsert.contradicting_relation_ids is not None
        else existing.contradicting_relation_ids
    )
    _validate_candidate_binding(
        candidate,
        supporting_relation_ids=support,
        contradicting_relation_ids=contradictions,
    )
    evidence, missing, requirements, grounding = _canonical_grounding(
        session,
        candidate=candidate,
        supporting_relation_ids=support,
        contradicting_relation_ids=contradictions,
    )
    return WorkingHypothesis(
        hypothesis_id=existing.hypothesis_id,
        semantic_key=existing.semantic_key,
        candidate_id=existing.candidate_id,
        claim=existing.claim,
        status=upsert.status or existing.status,
        supporting_relation_ids=support,
        contradicting_relation_ids=contradictions,
        evidence_ids=evidence,
        missing_reason_ids=missing,
        requirement_ids=requirements,
        grounding_refs=grounding,
        decision_summary=cleaned_summary or existing.decision_summary,
        next_check=(
            None
            if upsert.clear_next_check
            else cleaned_next_check
            if cleaned_next_check is not None
            else existing.next_check
        ),
        created_revision=existing.created_revision,
        updated_revision=next_revision,
    )


def _semantic_memory(memory: AgentWorkingMemory) -> dict[str, object]:
    payload = memory.model_dump(mode="json")
    payload.pop("revision", None)
    payload.pop("last_reconciled_knowledge_revision", None)
    payload.pop("last_reconciled_candidate_revision", None)
    for hypothesis in payload["hypotheses"].values():
        hypothesis.pop("created_revision", None)
        hypothesis.pop("updated_revision", None)
    return payload


def apply_working_memory_patch(
    session: CoreSession,
    patch: WorkingMemoryPatch,
) -> WorkingMemoryUpdateResult:
    current = session.working_memory
    if patch.expected_revision != current.revision:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=(
                f"working_memory_revision_conflict: expected {patch.expected_revision}, "
                f"current {current.revision}",
            ),
        )
    if not patch.upserts and patch.focus_hypothesis_id is None and not patch.clear_focus:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=("working_memory_patch_has_no_operations",),
        )

    next_revision = current.revision + 1
    hypotheses = dict(current.hypotheses)
    updated_ids: list[str] = []
    errors: list[str] = []
    for upsert in patch.upserts:
        if upsert.hypothesis_id is None:
            if not upsert.semantic_key:
                errors.append("new hypothesis requires semantic_key")
                continue
            generated_id = stable_id("hyp", upsert.semantic_key)
            if generated_id in hypotheses:
                errors.append(f"hypothesis already exists; update by ID: {generated_id}")
                continue
            existing = None
        else:
            existing = hypotheses.get(upsert.hypothesis_id)
            if existing is None:
                errors.append(f"unknown hypothesis ID: {upsert.hypothesis_id}")
                continue
        try:
            candidate = _build_hypothesis(
                session,
                existing,
                upsert,
                next_revision=next_revision,
            )
            grounding_errors = _status_grounding_errors(
                session,
                candidate,
                session.candidate_state.candidates.get(candidate.candidate_id or ""),
            )
            if grounding_errors:
                errors.extend(f"{candidate.hypothesis_id}: {error}" for error in grounding_errors)
                continue
        except ValueError as exc:
            errors.append(str(exc))
            continue
        hypotheses[candidate.hypothesis_id] = candidate
        updated_ids.append(candidate.hypothesis_id)

    focus = current.focus_hypothesis_id
    if patch.clear_focus:
        focus = None
    elif patch.focus_hypothesis_id is not None:
        focus = patch.focus_hypothesis_id
    elif focus is None and updated_ids:
        focus = updated_ids[0]
    if focus is not None and focus not in hypotheses:
        errors.append(f"unknown focus hypothesis ID: {focus}")
    if errors:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=tuple(errors),
        )
    if len(hypotheses) > MAX_WORKING_HYPOTHESES:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=(f"working_memory_hypothesis_limit_exceeded:{MAX_WORKING_HYPOTHESES}",),
        )

    candidate_memory = AgentWorkingMemory(
        revision=next_revision,
        focus_hypothesis_id=focus,
        hypotheses=dict(sorted(hypotheses.items())),
        last_reconciled_knowledge_revision=current.last_reconciled_knowledge_revision,
        last_reconciled_candidate_revision=current.last_reconciled_candidate_revision,
    )
    if _semantic_memory(candidate_memory) == _semantic_memory(current):
        return WorkingMemoryUpdateResult(
            accepted=True,
            changed=False,
            revision=current.revision,
        )
    if len(canonical_json(candidate_memory)) > MAX_WORKING_MEMORY_CHARACTERS:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=("working_memory_exceeds_character_limit",),
        )
    session.working_memory = candidate_memory
    return WorkingMemoryUpdateResult(
        accepted=True,
        changed=True,
        revision=candidate_memory.revision,
        updated_hypothesis_ids=tuple(updated_ids),
    )


def reconcile_working_memory(session: CoreSession) -> HypothesisReconciliationResult:
    current = session.working_memory
    if not current.hypotheses:
        if (
            current.last_reconciled_knowledge_revision == session.knowledge.revision
            and current.last_reconciled_candidate_revision == session.candidate_state.revision
        ):
            return HypothesisReconciliationResult(changed=False, revision=current.revision)
        session.working_memory = current.model_copy(
            update={
                "last_reconciled_knowledge_revision": session.knowledge.revision,
                "last_reconciled_candidate_revision": session.candidate_state.revision,
            }
        )
        return HypothesisReconciliationResult(changed=False, revision=current.revision)

    next_revision = current.revision + 1
    reconciled: dict[str, WorkingHypothesis] = {}
    ready: list[str] = []
    blocked: list[str] = []
    discarded: list[str] = []
    seen_grounding: set[tuple[str | None, tuple[str, ...], tuple[str, ...]]] = set()

    for hypothesis_id, hypothesis in sorted(current.hypotheses.items()):
        status = hypothesis.status
        summary = hypothesis.decision_summary
        next_check = hypothesis.next_check
        candidate = (
            session.candidate_state.candidates.get(hypothesis.candidate_id)
            if hypothesis.candidate_id is not None
            else None
        )
        relation_ids = (*hypothesis.supporting_relation_ids, *hypothesis.contradicting_relation_ids)
        missing_relations = sorted(set(relation_ids) - set(session.semantic.relations))
        grounding_key = (
            hypothesis.candidate_id,
            tuple(sorted(hypothesis.supporting_relation_ids)),
            tuple(sorted(hypothesis.contradicting_relation_ids)),
        )
        duplicate = grounding_key in seen_grounding
        if status is not WorkingHypothesisStatus.DISCARDED:
            seen_grounding.add(grounding_key)

        if status is not WorkingHypothesisStatus.DISCARDED:
            if duplicate:
                status = WorkingHypothesisStatus.DISCARDED
                summary = "Discarded automatically as duplicate grounded investigation."
                next_check = None
            elif missing_relations:
                status = WorkingHypothesisStatus.DISCARDED
                summary = "Discarded automatically after canonical relation recompute."
                next_check = None
            elif hypothesis.candidate_id is not None and candidate is None:
                status = WorkingHypothesisStatus.DISCARDED
                summary = "Discarded automatically because its canonical candidate disappeared."
                next_check = None
            elif candidate is not None and candidate.disposition in {
                CandidateDisposition.REJECTED,
                CandidateDisposition.MERGED,
            }:
                status = WorkingHypothesisStatus.DISCARDED
                summary = "Discarded automatically because its candidate is terminally rejected."
                next_check = None
            elif (
                candidate is not None
                and candidate.disposition is CandidateDisposition.BLOCKED_EXTERNAL
            ):
                status = WorkingHypothesisStatus.BLOCKED
                summary = "Blocked automatically by canonical candidate state."
                next_check = next_check or HypothesisNextCheck(
                    objective="Resolve the canonical blocked candidate state.",
                    target_ref=candidate.candidate_id,
                )
            else:
                support = [
                    session.semantic.relations[item]
                    for item in hypothesis.supporting_relation_ids
                ]
                contradictions = [
                    session.semantic.relations[item]
                    for item in hypothesis.contradicting_relation_ids
                ]
                if any(item.verdict.value == "FALSE" for item in support) or any(
                    item.verdict.value == "TRUE" for item in contradictions
                ):
                    status = WorkingHypothesisStatus.DISCARDED
                    summary = "Discarded automatically after canonical contradiction."
                    next_check = None
                elif any(item.verdict.value == "UNKNOWN" for item in (*support, *contradictions)):
                    status = WorkingHypothesisStatus.BLOCKED
                    summary = "Blocked automatically on unresolved canonical grounding."
                    if next_check is None:
                        unknown_relation = next(
                            item
                            for item in (*support, *contradictions)
                            if item.verdict.value == "UNKNOWN"
                        )
                        next_check = HypothesisNextCheck(
                            objective="Resolve the UNKNOWN canonical relation.",
                            target_ref=unknown_relation.relation_id,
                        )
                elif candidate is not None and candidate.current_claim_status in {
                    ClaimStatus.SUPPORTED,
                    ClaimStatus.CONFIRMED,
                }:
                    status = WorkingHypothesisStatus.READY_FOR_PROPOSAL
                    summary = "Canonical candidate now supports proposal review."
                    next_check = None
                elif support and all(item.verdict.value == "TRUE" for item in support) and any(
                    item.evidence_ids for item in support
                ):
                    status = WorkingHypothesisStatus.READY_FOR_PROPOSAL
                    summary = "Canonical relations now support proposal review."
                    next_check = None

        if missing_relations or (hypothesis.candidate_id is not None and candidate is None):
            evidence = hypothesis.evidence_ids
            missing = hypothesis.missing_reason_ids
            requirements = hypothesis.requirement_ids
            grounding = hypothesis.grounding_refs
        else:
            evidence, missing, requirements, grounding = _canonical_grounding(
                session,
                candidate=candidate,
                supporting_relation_ids=hypothesis.supporting_relation_ids,
                contradicting_relation_ids=hypothesis.contradicting_relation_ids,
            )
        updated = hypothesis.model_copy(
            update={
                "status": status,
                "evidence_ids": evidence,
                "missing_reason_ids": missing,
                "requirement_ids": requirements,
                "grounding_refs": grounding,
                "decision_summary": summary,
                "next_check": next_check,
            }
        )
        if updated.model_dump(mode="json") != hypothesis.model_dump(mode="json"):
            updated = updated.model_copy(update={"updated_revision": next_revision})
        reconciled[hypothesis_id] = WorkingHypothesis.model_validate(
            updated.model_dump(mode="python")
        )
        if status is WorkingHypothesisStatus.READY_FOR_PROPOSAL:
            ready.append(hypothesis_id)
        elif status is WorkingHypothesisStatus.BLOCKED:
            blocked.append(hypothesis_id)
        elif status is WorkingHypothesisStatus.DISCARDED:
            discarded.append(hypothesis_id)

    focus = current.focus_hypothesis_id
    if focus in discarded:
        focus = next(
            (
                item_id
                for item_id, item in reconciled.items()
                if item.status is not WorkingHypothesisStatus.DISCARDED
            ),
            None,
        )
    candidate_memory = AgentWorkingMemory(
        revision=next_revision,
        focus_hypothesis_id=focus,
        hypotheses=reconciled,
        last_reconciled_knowledge_revision=session.knowledge.revision,
        last_reconciled_candidate_revision=session.candidate_state.revision,
    )
    if len(canonical_json(candidate_memory)) > MAX_WORKING_MEMORY_CHARACTERS:
        raise WorkingMemoryLimitExceeded(
            "reconciled working memory exceeds the configured character limit"
        )
    if _semantic_memory(candidate_memory) == _semantic_memory(current):
        session.working_memory = candidate_memory.model_copy(update={"revision": current.revision})
        return HypothesisReconciliationResult(
            changed=False,
            revision=current.revision,
            ready_hypothesis_ids=tuple(ready),
            blocked_hypothesis_ids=tuple(blocked),
            discarded_hypothesis_ids=tuple(discarded),
        )
    session.working_memory = candidate_memory
    return HypothesisReconciliationResult(
        changed=True,
        revision=next_revision,
        ready_hypothesis_ids=tuple(ready),
        blocked_hypothesis_ids=tuple(blocked),
        discarded_hypothesis_ids=tuple(discarded),
    )
