"""Role- and target-bounded projection over the canonical Attack Graph."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from pydantic import BaseModel

from redteam.attack_graph.observation import GraphObservation, build_observation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.agents.definition import ContextProfile

MAX_EVIDENCE_SUMMARY_CHARS = 240


@dataclass(frozen=True)
class ProjectionCacheKey:
    namespace: str
    profile: str
    revision: int
    target_node_ids: tuple[str, ...]


class EvidenceExcerpt(BaseModel):
    """Small target-linked evidence reference safe for planner prompts.

    ``status_code``/``error_code`` are the RC#2 addition: the raw outcome the
    model needs to reason past a playbook -- e.g. "this returned 401" or
    "that failed with schema_mismatch" -- without leaking bodies, tokens or
    free-text error messages (only bounded, non-secret codes cross here).
    """

    evidence_id: str
    target_node_id: str
    content_type: str
    source_id: str
    summary: str | None = None
    hypothesis_id: str | None = None
    digest: str
    status_code: int | None = None
    error_code: str | None = None


def project_evidence_excerpts(
    records: list[EvidenceRecord],
    *,
    target_node_ids: list[GraphNodeId],
    limit: int,
) -> list[EvidenceExcerpt]:
    """Return only evidence explicitly associated with candidate targets.

    Untagged evidence is intentionally omitted: relevance cannot be inferred
    safely from free text, and unrelated evidence is worse than no excerpt.
    """

    if limit <= 0:
        return []
    targets = {str(item) for item in target_node_ids}
    associated = []
    for record in sorted(records, key=lambda item: (item.created_at, str(item.evidence_id))):
        target_id = str(record.metadata.get("target_node_id", ""))
        if target_id not in targets:
            continue
        observation = record.metadata.get("http_observation")
        status = observation.get("status_code") if isinstance(observation, dict) else None
        error = record.metadata.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        associated.append(
            EvidenceExcerpt(
                evidence_id=str(record.evidence_id),
                target_node_id=target_id,
                content_type=record.content_type,
                source_id=record.source_id,
                summary=(
                    record.summary[:MAX_EVIDENCE_SUMMARY_CHARS]
                    if record.summary is not None
                    else None
                ),
                hypothesis_id=(
                    str(record.hypothesis_id) if record.hypothesis_id is not None else None
                ),
                digest=record.digest,
                status_code=status if isinstance(status, int) else None,
                error_code=str(error_code) if isinstance(error_code, str) else None,
            )
        )
    return associated[-limit:]


class ContextProjectionCache:
    """Small shared LRU for checkpoint retries within one graph revision.

    The revision is deliberately part of the key.  This avoids serving a
    projection across graph mutations, while still eliminating repeated
    traversal when a durable LangGraph step is retried or resumed.
    """

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._items: OrderedDict[ProjectionCacheKey, GraphObservation] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: ProjectionCacheKey) -> GraphObservation | None:
        async with self._lock:
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return value.model_copy(deep=True)

    async def put(self, key: ProjectionCacheKey, value: GraphObservation) -> None:
        async with self._lock:
            self._items[key] = value.model_copy(deep=True)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    async def __len__(self) -> int:
        async with self._lock:
            return len(self._items)


class ContextProjector:
    """Implements located memory without creating a second memory store."""

    def __init__(
        self,
        graph: AttackGraphService,
        cache: ContextProjectionCache | None = None,
    ) -> None:
        self._graph = graph
        self._cache = cache

    async def project(
        self,
        profile: ContextProfile,
        *,
        step: int,
        target_node_ids: list[GraphNodeId],
        fallback: GraphObservation,
        revision: int | None = None,
        cache_namespace: str = "",
    ) -> GraphObservation:
        stable_targets = tuple(sorted(set(target_node_ids), key=str))
        cache_key = ProjectionCacheKey(
            namespace=cache_namespace,
            profile=profile.model_dump_json(),
            revision=step if revision is None else revision,
            target_node_ids=tuple(map(str, stable_targets)),
        )
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached.model_copy(update={"step": step}, deep=True)

        node_by_id = {}
        for target_id in stable_targets:
            target = await self._graph.get_node(target_id)
            if target is None:
                continue
            node_by_id[target.id] = target
            if profile.neighbor_depth:
                nodes, _ = await self._graph.subgraph_around(
                    target.id,
                    radius=profile.neighbor_depth,
                )
                node_by_id.update((node.id, node) for node in nodes)

        allowed = set(profile.include_node_types)
        nodes = [
            node
            for node in node_by_id.values()
            if not allowed or node.type in allowed
            if profile.include_hypotheses or node.type is not GraphNodeType.HYPOTHESIS
            if profile.include_findings or node.type is not GraphNodeType.FINDING
        ]
        if not nodes:
            projected = self._bounded_fallback(profile, fallback)
            if self._cache is not None:
                await self._cache.put(cache_key, projected)
            return projected

        # A conservative estimate keeps textual labels well under the profile's
        # token ceiling without pretending to be a model-specific tokenizer.
        token_limited_nodes = max(1, profile.max_tokens // 40)
        total_limit = min(profile.max_nodes, token_limited_nodes)
        projected = build_observation(
            nodes,
            step=step,
            max_per_type=total_limit,
            max_total=total_limit,
            max_attempted=total_limit if profile.include_attempted else 0,
        )
        projected.open_hypotheses = projected.open_hypotheses[: profile.max_evidence]
        projected.findings = projected.findings[: profile.max_evidence]
        if not profile.include_attempted:
            projected.attempted = []
        if self._cache is not None:
            await self._cache.put(cache_key, projected)
        return projected

    @staticmethod
    def _bounded_fallback(
        profile: ContextProfile, observation: GraphObservation
    ) -> GraphObservation:
        allowed = {item.value for item in profile.include_node_types}

        def type_allowed(name: str) -> bool:
            if allowed and name not in allowed:
                return False
            if not profile.include_hypotheses and name == GraphNodeType.HYPOTHESIS.value:
                return False
            if not profile.include_findings and name == GraphNodeType.FINDING.value:
                return False
            return True

        node_ids_by_type: dict[str, list[str]] = {}
        remaining = profile.max_nodes
        for name, ids in sorted(observation.node_ids_by_type.items()):
            if remaining <= 0:
                break
            if not type_allowed(name):
                continue
            node_ids_by_type[name] = ids[:remaining]
            remaining -= len(node_ids_by_type[name])
        nodes_by_type = {
            name: labels[: len(node_ids_by_type.get(name, []))]
            for name, labels in observation.nodes_by_type.items()
            if name in node_ids_by_type
        }
        return GraphObservation(
            step=observation.step,
            node_counts={
                name: count for name, count in observation.node_counts.items() if type_allowed(name)
            },
            nodes_by_type=nodes_by_type,
            node_ids_by_type=node_ids_by_type,
            open_hypotheses=(
                observation.open_hypotheses[: profile.max_evidence]
                if profile.include_hypotheses
                else []
            ),
            findings=(
                observation.findings[: profile.max_evidence] if profile.include_findings else []
            ),
            attempted=(
                observation.attempted[: profile.max_nodes] if profile.include_attempted else []
            ),
            truncated=observation.truncated,
        )


async def project_knowledge_section(
    graph_service: object,
    evidence_store: object,
    run_id: str,
    step: int = 0,
    target_node_ids: list[str] | None = None,
) -> dict:
    from redteam.orchestration.knowledge import project_knowledge

    return await project_knowledge(
        graph_service, evidence_store, str(run_id), step=step, target_node_ids=target_node_ids
    )


def format_knowledge_for_prompt(knowledge: dict) -> str:
    provided = knowledge.get("provided", {}) if isinstance(knowledge, dict) else {}
    facts = knowledge.get("facts", {}) if isinstance(knowledge, dict) else {}
    hypotheses = knowledge.get("hypotheses", {}) if isinstance(knowledge, dict) else {}
    findings = knowledge.get("findings", {}) if isinstance(knowledge, dict) else {}
    targets = knowledge.get("known_targets", {}) if isinstance(knowledge, dict) else {}
    lines: list[str] = []
    claims = provided.get("claims", []) if isinstance(provided, dict) else []
    lines.append(
        "provided("
        + str(len(claims))
        + "): declared scope and target notes, unconfirmed until observed"
    )
    for c in claims[:10]:
        if isinstance(c, dict):
            lines.append(
                "- "
                + str(c.get("node_id", "?"))
                + " ref="
                + str(c.get("target_ref"))
                + " verification="
                + str(c.get("verification", "?"))
            )
    omitted = provided.get("omitted_node_ids", []) if isinstance(provided, dict) else []
    if omitted:
        lines.append("provided omitted: " + ", ".join([str(v) for v in omitted[:10]]))
    if isinstance(facts, list):
        obs = facts
        excluded_count = (
            knowledge.get("excluded_inferred_attr_count", 0) if isinstance(knowledge, dict) else 0
        )
        fact_omitted = (
            knowledge.get("omitted_fact_node_ids", []) if isinstance(knowledge, dict) else []
        )
    else:
        obs = facts.get("observations", []) if isinstance(facts, dict) else []
        excluded_count = (
            facts.get("excluded_inferred_attr_count", 0) if isinstance(facts, dict) else 0
        )
        fact_omitted = facts.get("omitted_node_ids", []) if isinstance(facts, dict) else []
    lines.append("facts(" + str(len(obs)) + "): measured tool-result normalization only")
    for f in obs[:10]:
        if isinstance(f, dict):
            lines.append(
                "- "
                + str(f.get("node_id", "?"))
                + " ref="
                + str(f.get("fact_ref", "?"))
                + " attrs="
                + str(sorted((f.get("observed_attrs", {}) or {}).keys()))
            )
    lines.append("excluded inferred attrs: " + str(excluded_count))
    if fact_omitted:
        lines.append("facts omitted: " + ", ".join([str(v) for v in fact_omitted[:10]]))
    fact_refs = knowledge.get("fact_refs", []) if isinstance(knowledge, dict) else []
    if isinstance(fact_refs, list) and fact_refs:
        lines.append(
            "fact_refs(" + str(len(fact_refs)) + "): " + ", ".join([str(v) for v in fact_refs[:10]])
        )
    hyps = hypotheses.get("items", []) if isinstance(hypotheses, dict) else {}
    lines.append(
        "hypotheses("
        + str(len(hyps) if isinstance(hyps, list) else 0)
        + "): open questions, not facts"
    )
    for h in hyps[:10] if isinstance(hyps, list) else []:
        if isinstance(h, dict):
            lines.append(
                "- "
                + str(h.get("hypothesis_id", "?"))
                + " ["
                + str(h.get("status", "?"))
                + "] "
                + str(h.get("claim", ""))[:120]
            )
    finds = findings.get("items", []) if isinstance(findings, dict) else {}
    lines.append("findings(" + str(len(finds) if isinstance(finds, list) else 0) + ")")
    if isinstance(targets, list):
        tgts = targets
    else:
        tgts = targets.get("targets", []) if isinstance(targets, dict) else []
    lines.append(
        "known_targets("
        + str(len(tgts) if isinstance(tgts, list) else 0)
        + "): declared plus observed usable refs"
    )
    for t in tgts[:10] if isinstance(tgts, list) else []:
        if isinstance(t, str):
            lines.append("- " + t)
        elif isinstance(t, dict):
            lines.append(
                "- "
                + str(t.get("node_id", t.get("id", "?")))
                + " "
                + str(t.get("address", ""))[:120]
            )
    return "\n".join(lines)
