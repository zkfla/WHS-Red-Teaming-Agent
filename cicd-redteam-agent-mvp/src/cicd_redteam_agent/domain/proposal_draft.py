from __future__ import annotations

from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AnalysisProposalDraft,
    AnalysisState,
    AttackEdge,
    AttackPath,
    Finding,
    FindingStatus,
    Relation,
    TriState,
)


class ProposalDraftError(ValueError):
    pass


def relation_ref(index: int) -> str:
    return f"R{index + 1:04d}"


def indexed_relations(state: AnalysisState) -> list[tuple[str, Relation]]:
    return [
        (relation_ref(index), relation)
        for index, relation in enumerate(state.relations)
    ]


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _path_status(relations: list[Relation]) -> FindingStatus:
    if any(item.verdict is TriState.FALSE for item in relations):
        return FindingStatus.REJECTED
    if any(item.verdict is TriState.UNKNOWN for item in relations):
        return FindingStatus.CANDIDATE
    return FindingStatus.SUPPORTED


def expand_analysis_proposal_draft(
    state: AnalysisState, draft: AnalysisProposalDraft
) -> AnalysisProposal:
    """Expand short relation references into exact grounded proposal objects."""

    relation_index = dict(indexed_relations(state))
    paths: list[AttackPath] = []
    path_index: dict[str, AttackPath] = {}
    for path_draft in draft.path_drafts:
        if path_draft.path_id in path_index:
            raise ProposalDraftError(f"duplicate path_id: {path_draft.path_id}")
        if len(set(path_draft.relation_refs)) != len(path_draft.relation_refs):
            raise ProposalDraftError(
                f"path {path_draft.path_id} repeats a relation reference"
            )
        unknown_refs = [
            item for item in path_draft.relation_refs if item not in relation_index
        ]
        if unknown_refs:
            raise ProposalDraftError(
                f"path {path_draft.path_id} references unknown relations: {unknown_refs}"
            )
        relations = [relation_index[item] for item in path_draft.relation_refs]
        nodes = [relations[0].source_id]
        for relation in relations:
            if relation.source_id != nodes[-1]:
                raise ProposalDraftError(
                    f"path {path_draft.path_id} is not continuous at "
                    f"{relation.source_id}"
                )
            nodes.append(relation.target_id)
        edges = [
            AttackEdge(
                relation_id=relation.relation_id,
                source_id=relation.source_id,
                relation_type=relation.relation_type,
                target_id=relation.target_id,
                evidence_ids=relation.evidence_ids,
            )
            for relation in relations
        ]
        evidence_ids = _ordered_unique(
            [evidence_id for relation in relations for evidence_id in relation.evidence_ids]
        )
        missing_evidence = _ordered_unique(
            [missing for relation in relations for missing in relation.missing_evidence]
        )
        path = AttackPath(
            path_id=path_draft.path_id,
            nodes=nodes,
            edges=edges,
            status=_path_status(relations),
            evidence_ids=evidence_ids,
            missing_evidence=missing_evidence,
        )
        paths.append(path)
        path_index[path.path_id] = path

    findings: list[Finding] = []
    seen_finding_ids: set[str] = set()
    for finding_draft in draft.finding_drafts:
        if finding_draft.finding_id in seen_finding_ids:
            raise ProposalDraftError(
                f"duplicate finding_id: {finding_draft.finding_id}"
            )
        path = path_index.get(finding_draft.path_id)
        if path is None:
            raise ProposalDraftError(
                f"finding {finding_draft.finding_id} references unknown path: "
                f"{finding_draft.path_id}"
            )
        findings.append(
            Finding(
                finding_id=finding_draft.finding_id,
                rule_id=finding_draft.rule_id,
                title=finding_draft.title,
                status=path.status,
                evidence_ids=path.evidence_ids,
                missing_evidence=path.missing_evidence,
                confidence=finding_draft.confidence,
                summary=finding_draft.summary,
            )
        )
        seen_finding_ids.add(finding_draft.finding_id)

    return AnalysisProposal(findings=findings, paths=paths)
