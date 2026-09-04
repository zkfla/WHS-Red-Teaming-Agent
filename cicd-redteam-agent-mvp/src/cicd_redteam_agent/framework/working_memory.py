from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from hackingBuddyGPT.capability import Capability
from pydantic import BaseModel, Field, create_model

from cicd_redteam_agent.domain.graph_frontier import (
    FrontierDisposition,
    FrontierSearchResult,
)
from cicd_redteam_agent.domain.models import (
    AgentWorkingMemory,
    AnalysisState,
    CandidateDisposition,
    FindingStatus,
    WorkingHypothesis,
    WorkingHypothesisStatus,
    WorkingHypothesisUpsert,
    WorkingMemoryPatch,
    WorkingMemoryUpdateResult,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.security.redaction import redact

MAX_WORKING_MEMORY_CHARACTERS = 12_000
MAX_PATCH_CHARACTERS = 12_000
FOLLOW_UP_INVESTIGATION_CAPABILITIES = {
    "GetWorkflowFacts",
    "InspectRepositoryFile",
    "SearchRepository",
}


def _normalize_claim(claim: str) -> str:
    return " ".join(claim.split()).casefold()


def _hypothesis_id(repository_id: str, claim: str) -> str:
    digest = hashlib.sha256(f"{repository_id}|{_normalize_claim(claim)}".encode()).hexdigest()[:16]
    return f"hyp_{digest}"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return redact(value).strip()


def _clean_upsert(upsert: WorkingHypothesisUpsert) -> WorkingHypothesisUpsert:
    payload = upsert.model_dump()
    for field_name in ("claim", "decision_summary"):
        payload[field_name] = _clean_text(payload[field_name])
    if upsert.next_check is not None:
        next_check = upsert.next_check.model_dump()
        for field_name in ("objective", "target"):
            next_check[field_name] = _clean_text(next_check[field_name])
        payload["next_check"] = next_check
    if upsert.missing_evidence is not None:
        payload["missing_evidence"] = [
            cleaned for item in upsert.missing_evidence if (cleaned := _clean_text(item))
        ]
    for field_name in (
        "supporting_relation_ids",
        "contradicting_relation_ids",
        "evidence_ids",
        "missing_evidence",
    ):
        if payload[field_name] is not None:
            payload[field_name] = list(dict.fromkeys(payload[field_name]))
    return WorkingHypothesisUpsert.model_validate(payload)


def sanitize_working_memory_patch_json(value: str) -> str:
    """Redact only free-text patch fields while preserving exact grounding IDs."""

    try:
        patch = WorkingMemoryPatch.model_validate_json(value)
    except Exception:
        return redact(value)
    payload = patch.model_dump()
    payload["upserts"] = [_clean_upsert(item).model_dump() for item in patch.upserts]
    return WorkingMemoryPatch.model_validate(payload).model_dump_json()


def expand_working_memory_patch_refs(state: AnalysisState, value: str) -> WorkingMemoryPatch:
    """Resolve run-local relation refs and derive their grounding fields."""

    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("working memory patch must be a JSON object")
    upserts = payload.get("upserts", [])
    if not isinstance(upserts, list):
        raise ValueError("working memory patch upserts must be a list")

    relation_index = dict(indexed_relations(state))
    for upsert in upserts:
        if not isinstance(upsert, dict):
            raise ValueError("working memory patch upserts must be objects")
        referenced_relations = []
        for ref_field, id_field in (
            ("supporting_relation_refs", "supporting_relation_ids"),
            ("contradicting_relation_refs", "contradicting_relation_ids"),
        ):
            refs = upsert.pop(ref_field, None)
            if refs is None:
                continue
            if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
                raise ValueError(f"{ref_field} must be a list of strings")
            unknown_refs = [item for item in refs if item not in relation_index]
            if unknown_refs:
                raise ValueError(f"unknown relation refs: {unknown_refs}")
            relations = [relation_index[item] for item in refs]
            referenced_relations.extend(relations)
            existing_ids = upsert.get(id_field) or []
            upsert[id_field] = list(
                dict.fromkeys([*existing_ids, *(relation.relation_id for relation in relations)])
            )

        if referenced_relations:
            existing_evidence = upsert.get("evidence_ids") or []
            existing_missing = upsert.get("missing_evidence") or []
            upsert["evidence_ids"] = list(
                dict.fromkeys(
                    [
                        *existing_evidence,
                        *(
                            evidence_id
                            for relation in referenced_relations
                            for evidence_id in relation.evidence_ids
                        ),
                    ]
                )
            )
            upsert["missing_evidence"] = list(
                dict.fromkeys(
                    [
                        *existing_missing,
                        *(
                            missing
                            for relation in referenced_relations
                            for missing in relation.missing_evidence
                        ),
                    ]
                )
            )
    return WorkingMemoryPatch.model_validate(payload)


def _semantic_memory(memory: AgentWorkingMemory) -> dict[str, Any]:
    payload = memory.model_dump()
    payload.pop("revision", None)
    for hypothesis in payload["hypotheses"]:
        hypothesis.pop("created_revision", None)
        hypothesis.pop("updated_revision", None)
    return payload


@dataclass(frozen=True)
class HypothesisReconciliationResult:
    changed: bool
    revision: int
    ready_hypothesis_ids: tuple[str, ...] = ()
    discarded_hypothesis_ids: tuple[str, ...] = ()


def reconcile_working_memory(
    state: AnalysisState,
    frontiers: FrontierSearchResult,
) -> HypothesisReconciliationResult:
    """Reconcile durable hypotheses against the current canonical Frontier graph."""

    memory = state.working_memory
    if not memory.hypotheses:
        return HypothesisReconciliationResult(changed=False, revision=memory.revision)

    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    relation_by_ref = dict(indexed_relations(state))
    candidate_by_id = {candidate.candidate_id: candidate for candidate in state.candidates}

    def relation_ids(frontier) -> frozenset[str]:
        return frozenset(
            relation.relation_id
            for reference in frontier.relation_refs
            if (relation := relation_by_ref.get(reference)) is not None
        )

    ready_sets = [
        relation_ids(frontier) for frontier in frontiers.frontiers if frontier.proposal_ready
    ]
    blocked_sets = [
        relation_ids(frontier)
        for frontier in frontiers.frontiers
        if frontier.disposition is FrontierDisposition.BLOCKED
    ]
    viable_sets = [
        relation_ids(frontier)
        for frontier in frontiers.frontiers
        if frontier.disposition is not FrontierDisposition.BLOCKED
    ]

    next_revision = memory.revision + 1
    seen_grounding: set[tuple[str, ...]] = set()
    reconciled: list[WorkingHypothesis] = []
    ready_ids: list[str] = []
    discarded_ids: list[str] = []
    for hypothesis in memory.hypotheses:
        payload = hypothesis.model_dump()
        original_support = tuple(hypothesis.supporting_relation_ids)
        support = tuple(item for item in original_support if item in relation_by_id)
        contradictions = tuple(
            item for item in hypothesis.contradicting_relation_ids if item in relation_by_id
        )
        support_set = frozenset(support)
        grounding_key = tuple(sorted(support))
        duplicate = bool(support) and grounding_key in seen_grounding
        if support and hypothesis.status is not WorkingHypothesisStatus.DISCARDED:
            seen_grounding.add(grounding_key)

        status = hypothesis.status
        decision_summary = hypothesis.decision_summary
        clear_next_check = False
        bound_candidate = (
            candidate_by_id.get(hypothesis.candidate_id)
            if hypothesis.candidate_id is not None
            else None
        )
        candidate_missing = hypothesis.candidate_id is not None and bound_candidate is None
        candidate_blocked = bool(
            bound_candidate is not None
            and bound_candidate.disposition is CandidateDisposition.BLOCKED_EXTERNAL
        )
        candidate_discarded = bool(
            bound_candidate is not None
            and bound_candidate.disposition
            in {CandidateDisposition.REJECTED, CandidateDisposition.MERGED}
        )
        candidate_ready = bool(
            bound_candidate is not None
            and bound_candidate.status is not FindingStatus.REJECTED
            and bound_candidate.path_variants
        )
        stale_grounding = len(support) != len(original_support)
        hard_contradiction = any(
            relation_by_id[item].verdict.value == "FALSE" for item in support
        ) or any(relation_by_id[item].verdict.value == "TRUE" for item in contradictions)
        support_has_evidence = any(relation_by_id[item].evidence_ids for item in support)
        covered_by_ready = (
            bool(support_set)
            and support_has_evidence
            and any(support_set <= candidate for candidate in ready_sets)
        )
        covered_by_block = bool(support_set) and any(
            support_set <= candidate for candidate in blocked_sets
        )
        covered_by_viable = bool(support_set) and any(
            support_set <= candidate for candidate in viable_sets
        )

        if hypothesis.status is not WorkingHypothesisStatus.DISCARDED:
            if duplicate:
                status = WorkingHypothesisStatus.DISCARDED
                decision_summary = "Discarded automatically as duplicate grounded investigation."
                clear_next_check = True
            elif stale_grounding or hard_contradiction:
                status = WorkingHypothesisStatus.DISCARDED
                decision_summary = "Discarded automatically after canonical semantic recompute."
                clear_next_check = True
            elif candidate_missing or candidate_discarded:
                status = WorkingHypothesisStatus.DISCARDED
                decision_summary = (
                    "Discarded automatically because its canonical candidate disappeared "
                    "or was merged/rejected."
                )
                clear_next_check = True
            elif candidate_blocked:
                status = WorkingHypothesisStatus.BLOCKED
                decision_summary = (
                    "Blocked automatically by the candidate's external evidence state."
                )
                clear_next_check = True
            elif candidate_ready:
                status = WorkingHypothesisStatus.READY_FOR_PROPOSAL
                decision_summary = "Canonical SecurityCandidate now supports proposal review."
                clear_next_check = True
            elif covered_by_ready:
                status = WorkingHypothesisStatus.READY_FOR_PROPOSAL
                decision_summary = "Canonical Frontier now supports proposal review."
                clear_next_check = True
            elif covered_by_block and not covered_by_viable:
                status = WorkingHypothesisStatus.DISCARDED
                decision_summary = "Discarded automatically because its execution path is blocked."
                clear_next_check = True
            elif (
                support_set
                and frontiers.search_complete
                and not frontiers.truncated
                and not covered_by_viable
            ):
                status = WorkingHypothesisStatus.DISCARDED
                decision_summary = "Discarded automatically because no viable Frontier retains it."
                clear_next_check = True

        referenced = [relation_by_id[item] for item in (*support, *contradictions)]
        payload.update(
            {
                "status": status,
                "supporting_relation_ids": list(support),
                "contradicting_relation_ids": list(contradictions),
                "evidence_ids": list(
                    dict.fromkeys(
                        evidence_id
                        for relation in referenced
                        for evidence_id in relation.evidence_ids
                    )
                ),
                "missing_evidence": list(
                    dict.fromkeys(
                        [
                            *(
                                bound_candidate.missing_evidence
                                if bound_candidate is not None
                                else []
                            ),
                            *(
                                missing
                                for relation in referenced
                                if relation.verdict.value == "UNKNOWN"
                                for missing in relation.missing_evidence
                            ),
                        ]
                    )
                ),
                "decision_summary": decision_summary,
                "next_check": None if clear_next_check else hypothesis.next_check,
            }
        )
        candidate = WorkingHypothesis.model_validate(payload)
        if candidate.model_dump() != hypothesis.model_dump():
            payload["updated_revision"] = next_revision
            candidate = WorkingHypothesis.model_validate(payload)
        reconciled.append(candidate)
        if candidate.status is WorkingHypothesisStatus.READY_FOR_PROPOSAL:
            ready_ids.append(candidate.hypothesis_id)
        if candidate.status is WorkingHypothesisStatus.DISCARDED:
            discarded_ids.append(candidate.hypothesis_id)

    focus = memory.focus_hypothesis_id
    if focus in discarded_ids:
        focus = next(
            (
                item.hypothesis_id
                for item in reconciled
                if item.status is not WorkingHypothesisStatus.DISCARDED
            ),
            None,
        )
    candidate_memory = AgentWorkingMemory(
        revision=next_revision,
        focus_hypothesis_id=focus,
        hypotheses=reconciled,
    )
    if _semantic_memory(candidate_memory) == _semantic_memory(memory):
        return HypothesisReconciliationResult(
            changed=False,
            revision=memory.revision,
            ready_hypothesis_ids=tuple(ready_ids),
            discarded_hypothesis_ids=tuple(discarded_ids),
        )
    state.working_memory = candidate_memory
    return HypothesisReconciliationResult(
        changed=True,
        revision=next_revision,
        ready_hypothesis_ids=tuple(ready_ids),
        discarded_hypothesis_ids=tuple(discarded_ids),
    )


def _merge_hypothesis(
    existing: WorkingHypothesis | None,
    upsert: WorkingHypothesisUpsert,
    *,
    repository_id: str,
    next_revision: int,
) -> WorkingHypothesis:
    cleaned = _clean_upsert(upsert)
    if existing is None:
        if not cleaned.claim or not cleaned.decision_summary:
            raise ValueError("new hypothesis requires claim and decision_summary")
        hypothesis_id = _hypothesis_id(repository_id, cleaned.claim)
        return WorkingHypothesis(
            hypothesis_id=hypothesis_id,
            candidate_id=cleaned.candidate_id,
            claim=cleaned.claim,
            status=cleaned.status or WorkingHypothesisStatus.ACTIVE,
            supporting_relation_ids=cleaned.supporting_relation_ids or [],
            contradicting_relation_ids=cleaned.contradicting_relation_ids or [],
            evidence_ids=cleaned.evidence_ids or [],
            missing_evidence=cleaned.missing_evidence or [],
            decision_summary=cleaned.decision_summary,
            next_check=None if cleaned.clear_next_check else cleaned.next_check,
            created_revision=next_revision,
            updated_revision=next_revision,
        )

    if cleaned.claim and _normalize_claim(cleaned.claim) != _normalize_claim(existing.claim):
        raise ValueError(f"hypothesis claim is immutable: {existing.hypothesis_id}")
    if cleaned.candidate_id and cleaned.candidate_id != existing.candidate_id:
        raise ValueError(f"hypothesis candidate binding is immutable: {existing.hypothesis_id}")
    payload = existing.model_dump()
    for field_name in (
        "status",
        "supporting_relation_ids",
        "contradicting_relation_ids",
        "evidence_ids",
        "missing_evidence",
        "decision_summary",
    ):
        value = getattr(cleaned, field_name)
        if value is not None:
            payload[field_name] = value
    if cleaned.candidate_id is not None:
        payload["candidate_id"] = cleaned.candidate_id
    if cleaned.clear_next_check:
        payload["next_check"] = None
    elif cleaned.next_check is not None:
        payload["next_check"] = cleaned.next_check
    payload["updated_revision"] = next_revision
    return WorkingHypothesis.model_validate(payload)


def _validate_hypothesis(
    hypothesis: WorkingHypothesis,
    state: AnalysisState,
    allowed_capabilities: set[str],
) -> list[str]:
    errors: list[str] = []
    known_evidence = set(state.evidence)
    relation_index = {relation.relation_id: relation for relation in state.relations}
    relation_ids = [
        *hypothesis.supporting_relation_ids,
        *hypothesis.contradicting_relation_ids,
    ]
    unknown_evidence = set(hypothesis.evidence_ids) - known_evidence
    unknown_relations = set(relation_ids) - set(relation_index)
    if unknown_evidence:
        errors.append(f"unknown evidence IDs: {sorted(unknown_evidence)}")
    if unknown_relations:
        errors.append(f"unknown relation IDs: {sorted(unknown_relations)}")
    if hypothesis.candidate_id:
        candidate = next(
            (
                item
                for item in state.candidates
                if item.candidate_id == hypothesis.candidate_id
            ),
            None,
        )
        if candidate is None:
            errors.append(f"unknown candidate ID: {hypothesis.candidate_id}")
        else:
            allowed = set(candidate.supporting_relation_ids) | set(
                candidate.contradicting_relation_ids
            )
            outside = set(relation_ids) - allowed
            if outside:
                errors.append(
                    f"candidate-bound hypothesis references unrelated relations: {sorted(outside)}"
                )
    false_support = [
        relation_id
        for relation_id in hypothesis.supporting_relation_ids
        if relation_id in relation_index and relation_index[relation_id].verdict.value == "FALSE"
    ]
    if false_support:
        errors.append(f"FALSE relations cannot support a hypothesis: {sorted(false_support)}")
    overlap = set(hypothesis.supporting_relation_ids) & set(hypothesis.contradicting_relation_ids)
    if overlap:
        errors.append(f"relations cannot both support and contradict: {sorted(overlap)}")
    referenced_relations = [
        relation_index[relation_id] for relation_id in relation_ids if relation_id in relation_index
    ]
    required_evidence = {
        evidence_id for relation in referenced_relations for evidence_id in relation.evidence_ids
    }
    omitted_evidence = required_evidence - set(hypothesis.evidence_ids)
    if omitted_evidence:
        errors.append(f"hypothesis omits relation evidence: {sorted(omitted_evidence)}")
    required_missing = {
        missing
        for relation in referenced_relations
        if relation.verdict.value == "UNKNOWN"
        for missing in relation.missing_evidence
    }
    omitted_missing = required_missing - set(hypothesis.missing_evidence)
    if omitted_missing:
        errors.append(
            f"hypothesis omits UNKNOWN relation missing evidence: {sorted(omitted_missing)}"
        )
    if hypothesis.status is WorkingHypothesisStatus.READY_FOR_PROPOSAL and not (
        hypothesis.supporting_relation_ids and hypothesis.evidence_ids
    ):
        errors.append("READY_FOR_PROPOSAL requires supporting relations and evidence")
    if hypothesis.status is WorkingHypothesisStatus.BLOCKED and not (
        hypothesis.missing_evidence or hypothesis.next_check
    ):
        errors.append("BLOCKED requires missing_evidence or next_check")
    if hypothesis.status is WorkingHypothesisStatus.DISCARDED and hypothesis.next_check:
        errors.append("DISCARDED hypothesis cannot retain next_check")
    if (
        hypothesis.next_check
        and hypothesis.next_check.preferred_capability
        and hypothesis.next_check.preferred_capability not in allowed_capabilities
    ):
        errors.append(f"unknown preferred capability: {hypothesis.next_check.preferred_capability}")
    return errors


def apply_working_memory_patch(
    state: AnalysisState,
    patch: WorkingMemoryPatch,
    *,
    allowed_capabilities: set[str],
) -> WorkingMemoryUpdateResult:
    current = state.working_memory
    if patch.expected_revision != current.revision:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=[
                f"working_memory_revision_conflict: expected {patch.expected_revision}, "
                f"current {current.revision}"
            ],
        )
    if not patch.upserts and patch.focus_hypothesis_id is None and not patch.clear_focus:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=["working_memory_patch_has_no_operations"],
        )

    next_revision = current.revision + 1
    hypotheses = {item.hypothesis_id: item for item in current.hypotheses}
    updated_ids: list[str] = []
    errors: list[str] = []
    for raw_upsert in patch.upserts:
        if raw_upsert.hypothesis_id is None and raw_upsert.claim:
            generated_id = _hypothesis_id(state.repository.repository_id, raw_upsert.claim)
            if generated_id in hypotheses:
                errors.append(f"hypothesis already exists; update by ID: {generated_id}")
                continue
        existing = hypotheses.get(raw_upsert.hypothesis_id or "")
        if raw_upsert.hypothesis_id and existing is None:
            errors.append(f"unknown hypothesis ID: {raw_upsert.hypothesis_id}")
            continue
        try:
            merged = _merge_hypothesis(
                existing,
                raw_upsert,
                repository_id=state.repository.repository_id,
                next_revision=next_revision,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        hypothesis_errors = _validate_hypothesis(merged, state, allowed_capabilities)
        if hypothesis_errors:
            errors.extend(f"{merged.hypothesis_id}: {error}" for error in hypothesis_errors)
            continue
        hypotheses[merged.hypothesis_id] = merged
        updated_ids.append(merged.hypothesis_id)

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
            errors=errors,
        )

    candidate = AgentWorkingMemory(
        revision=next_revision,
        focus_hypothesis_id=focus,
        hypotheses=list(hypotheses.values()),
    )
    current_semantic = _semantic_memory(current)
    candidate_semantic = _semantic_memory(candidate)
    if current_semantic == candidate_semantic:
        return WorkingMemoryUpdateResult(
            accepted=True,
            changed=False,
            revision=current.revision,
            updated_hypothesis_ids=[],
        )
    if len(candidate.model_dump_json()) > MAX_WORKING_MEMORY_CHARACTERS:
        return WorkingMemoryUpdateResult(
            accepted=False,
            revision=current.revision,
            errors=["working_memory_exceeds_character_limit"],
        )
    state.working_memory = candidate
    return WorkingMemoryUpdateResult(
        accepted=True,
        changed=True,
        revision=candidate.revision,
        updated_hypothesis_ids=updated_ids,
    )


@dataclass
class MemoryAwareCapability(Capability):
    inner: Capability
    state: AnalysisState
    lock: Any
    allowed_capabilities: set[str]

    def get_name(self) -> str:
        return self.inner.get_name()

    def describe(self) -> str:
        return (
            f"{self.inner.describe()} Optionally include working_memory_patch_json to update "
            "bounded investigation hypotheses in the same round before this action runs. "
            "Prefer supporting_relation_refs and contradicting_relation_refs from "
            "ListAttackFrontiers or ListSecurityFacts; the runtime restores exact Relation and "
            "Evidence fields."
        )

    async def __call__(self, *args, **kwargs) -> str:
        return await self.inner(*args, **kwargs)

    def to_model(self) -> type[BaseModel]:
        inner_model = self.inner.to_model()
        model_type = create_model(
            self.get_name(),
            __base__=inner_model,
            working_memory_patch_json=(
                str | None,
                Field(
                    default=None,
                    max_length=MAX_PATCH_CHARACTERS,
                    description=(
                        "Optional JSON-encoded WorkingMemoryPatch. Use only for a material "
                        "hypothesis change; omit it otherwise."
                    ),
                ),
            ),
        )

        async def execute(model):
            arguments = model.model_dump(exclude={"working_memory_patch_json"})
            update_result: WorkingMemoryUpdateResult | None = None
            investigation_basis_was_listed = any(
                item.startswith(("attack-frontiers:", "security-facts:"))
                for item in self.state.inspected_resources
            )
            if (
                investigation_basis_was_listed
                and self.get_name() in FOLLOW_UP_INVESTIGATION_CAPABILITIES
                and self.state.working_memory.revision == 0
                and not model.working_memory_patch_json
            ):
                return json.dumps(
                    {
                        "error": "working_memory_patch_required_for_follow_up_investigation",
                        "guidance": (
                            "Attack frontiers or security facts are already available. Attach an "
                            "ACTIVE or BLOCKED hypothesis with the concrete next check to this "
                            "capability call."
                        ),
                    }
                )
            if model.working_memory_patch_json:
                try:
                    patch = expand_working_memory_patch_refs(
                        self.state, model.working_memory_patch_json
                    )
                except Exception as exc:
                    return json.dumps(
                        {
                            "error": "invalid_working_memory_patch",
                            "detail": str(exc),
                        }
                    )
                async with self.lock:
                    update_result = apply_working_memory_patch(
                        self.state,
                        patch,
                        allowed_capabilities=self.allowed_capabilities,
                    )
                if not update_result.accepted:
                    return json.dumps(
                        {
                            "error": "working_memory_patch_rejected",
                            "working_memory_update": update_result.model_dump(mode="json"),
                        }
                    )

            capability_result = await self.inner(**arguments)
            if update_result is None:
                return capability_result
            try:
                parsed_result: Any = json.loads(capability_result)
            except (json.JSONDecodeError, TypeError):
                parsed_result = capability_result
            return json.dumps(
                {
                    "working_memory_update": update_result.model_dump(mode="json"),
                    "capability_result": parsed_result,
                }
            )

        model_type.execute = execute
        return model_type


def wrap_capabilities_with_memory(
    capabilities: list[Capability], state: AnalysisState, lock: Any
) -> list[Capability]:
    allowed = {capability.get_name() for capability in capabilities}
    return [MemoryAwareCapability(item, state, lock, allowed) for item in capabilities]
