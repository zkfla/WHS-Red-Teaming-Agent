"""Role- and target-bounded projection over the canonical Attack Graph."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.observation import GraphObservation, build_observation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.agents.definition import ContextProfile

MAX_EVIDENCE_SUMMARY_CHARS = 240


def completion_review_context(context: dict[str, Any] | None, evidence_ids: list[str]) -> dict:
    """Keep review constraints and already-read windows, without planning history.

    Review calls have no tool loop of their own. Relevant requested windows
    therefore remain alongside the caller's source excerpts; references alone
    must never be described as content the reviewer has read.
    """
    from redteam.core.contracts.mission import redact_secret_material

    current = context or {}
    projected: dict[str, Any] = {
        key: current[key]
        for key in ("engagement_constraints", "operator_instructions", "evidence_read_contract")
        if key in current
    }
    mission = current.get("mission")
    if isinstance(mission, dict):
        projected["operator_constraints"] = mission.get("operator_constraints", {})
    sources = set(map(str, evidence_ids))
    projected["requested_evidence"] = [
        window for window in current.get("requested_evidence", [])
        if isinstance(window, dict) and str(window.get("evidence_id", "")) in sources
    ]
    knowledge = current.get("knowledge")
    if isinstance(knowledge, dict):
        projected["knowledge"] = {
            "facts": [
                item for item in knowledge.get("facts", [])
                if sources.intersection(map(str, item.get("grounded_evidence_ids", [])))
            ],
            **{
                key: knowledge[key]
                for key in (
                    "omitted_fact_node_ids", "evidence_diagnostics", "graph_diagnostics",
                )
                if key in knowledge
            },
        }
    projected["local_tool_results"] = [
        item for item in current.get("local_tool_results", [])
        if sources.intersection(map(str, item.get("result", {}).get("refs", [])))
    ]
    projected["recording_context_note"] = (
        "knowledge.facts is the current public view of registered observations, with "
        "per-claim provenance. local_tool_results reports related internal calls "
        "and their own effects, including partial acceptance and refusal; these are "
        "separate from target "
        "tool outputs and completion admission. This view is capped: missing facts "
        "are not proof of absence; retain omission and lookup diagnostics."
    )
    return redact_secret_material(projected)


def review_path_reference(path: Any) -> dict[str, Any]:
    """Identify the reviewed snapshot without repeating its proposed methods."""
    return {
        key: getattr(path, key, None)
        for key in ("path_id", "run_id", "mission_id", "mission_task_id", "version", "status")
    }


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
    processing: dict[str, Any] = Field(default_factory=dict)


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
    processing = project_run_processing(records)
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
                processing=processing.get(str(record.evidence_id), {}),
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


class MappingStatus(BaseModel):
    """One preserved call: how mapping, processing and reflection went.

    proposed_keys lists the metadata public keys a caller may ask for; only
    keys actually present on the record travel back, never guessed values.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    mapping_status: str = "unknown"
    mapping_outcome: str | None = None
    mapping_tool: str | None = None
    mapping_reason: str | None = None
    processing_status: str = "unknown"
    processing_stage: str | None = None
    processing_outcome: str | None = None
    recovery_status: str | None = None
    reference_count: int = 0
    attempted: dict[str, int] = Field(default_factory=dict)
    confirmed: dict[str, int] = Field(default_factory=dict)
    failed_at: str | None = None
    processing_attempt: int | None = None
    processing_conflict: bool = False
    processing_duplicate_count: int = 0
    proposed_keys: list[str] = Field(default_factory=list)


def _public_status_value(metadata: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = metadata.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def project_mapping_status(*, record: Any, outcome: Any | None = None) -> MappingStatus:
    """Project the same status contract the root public layer reads.

    Reads only metadata keys root already exposes; never rewrites the
    original tool_result or any preserved raw payload.
    """
    metadata = getattr(record, "metadata", None)
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    references = meta.get("target_references")
    reference_count = len(list(references)) if isinstance(references, list) else 0
    record_kind = _public_status_value(meta, "record_kind")
    stage = _public_status_value(meta, "processing_stage", "stage", "admission", "receipt_kind")
    status = _public_status_value(
        meta, "processing_status", "processing_outcome", "recovery_status"
    )
    if status is None and record_kind in {
        "target_references",
        "mapper_failure",
        "recovery_result",
        "recovery_note",
        "processing_attempt",
    }:
        status = record_kind
    proposed = [
        "mapping_status",
        "mapping_outcome",
        "processing_status",
        "processing_stage",
        "processing_outcome",
        "recovery_status",
        "target_references",
    ]
    mapping_status: str | None
    mapping_outcome: str | None
    if outcome is not None:
        mapping_status = str(getattr(outcome, "status", "unknown"))
        mapping_outcome = str(getattr(outcome, "status", "unknown"))
        mapping_tool = getattr(outcome, "mapper", None)
        mapping_reason = getattr(outcome, "reason", None)
        if isinstance(mapping_tool, str) and not mapping_tool.strip():
            mapping_tool = None
        if isinstance(mapping_reason, str) and not mapping_reason.strip():
            mapping_reason = None
    else:
        mapping_status = _public_status_value(meta, "mapping_status", "mapping_outcome")
        if mapping_status is None:
            mapping_status = "stored" if reference_count else "unknown"
        mapping_outcome = _public_status_value(meta, "mapping_outcome", "mapping_status")
        mapping_tool = _public_status_value(meta, "tool_id", "mapper")
        mapping_reason = _public_status_value(meta, "reason", "error")
    return MappingStatus(
        evidence_id=str(getattr(record, "evidence_id", "")),
        mapping_status=mapping_status or "unknown",
        mapping_outcome=mapping_outcome,
        mapping_tool=mapping_tool,
        mapping_reason=mapping_reason,
        processing_status=status or "unknown",
        processing_stage=stage,
        processing_outcome=status,
        recovery_status=_public_status_value(meta, "recovery_status"),
        reference_count=reference_count,
        attempted=meta.get("attempted", {}),
        confirmed=meta.get("confirmed", {}),
        failed_at=_public_status_value(meta, "failed_at"),
        processing_attempt=meta.get("processing_attempt"),
        processing_conflict=meta.get("processing_conflict", False),
        processing_duplicate_count=meta.get("processing_duplicate_count", 0),
        proposed_keys=[key for key in proposed if key in meta],
    )


def project_run_processing(records: list[EvidenceRecord]) -> dict[str, dict[str, Any]]:
    """Join system processing companions to preserved calls for prompt/report display.

    This is a read-only view, never evidence admission or graph observation.
    Companion fields can advance an apply/recovery status without rewriting
    the source payload. Unfinished attempts remain explicitly attempted.
    """
    from redteam.core.enums import EvidenceSourceType
    from redteam.orchestration.evidence_access import raw_from_metadata

    sources = {
        str(record.evidence_id): record
        for record in records
        if raw_from_metadata(record.metadata) is not None
    }
    companions: dict[str, list[dict[str, Any]]] = {identity: [] for identity in sources}
    for companion in records:
        source_id = str(companion.metadata.get("source_evidence_id", ""))
        source = sources.get(source_id)
        if source is None or companion.source_type is not EvidenceSourceType.SYSTEM:
            continue
        if any(
            getattr(companion, key) != getattr(source, key)
            for key in ("run_id", "engagement_id", "action_id", "invocation_id")
        ):
            continue
        companions[source_id].append(companion.metadata)
    return {
        identity: project_mapping_status(
            record=source.model_copy(
                update={"metadata": _processing_view(source.metadata, companions[identity])}
            )
        ).model_dump(mode="json")
        for identity, source in sources.items()
    }


def _processing_view(source: dict[str, Any], companions: list[dict[str, Any]]) -> dict[str, Any]:
    """Select a linked attempt and reject conflicting endings independent of clocks."""
    import json

    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in companions:
        attempt = item.get("attempt")
        if type(attempt) is int and attempt >= 1:
            grouped.setdefault(attempt, []).append(item)
    if not grouped:
        return dict(source)
    attempt = max(grouped)
    selected = grouped[attempt]
    view = {
        key: value
        for key, value in source.items()
        if not key.startswith("processing_") and key not in {"attempted", "confirmed", "failed_at"}
    }
    view["processing_attempt"] = attempt
    previous = max((item for item in grouped if item < attempt), default=1)
    links = {item.get("supersedes_attempt") for item in selected if "supersedes_attempt" in item}
    conflict = attempt > 1 and links != {previous}
    meaningful = (
        "mapping_status",
        "mapping_outcome",
        "processing_status",
        "processing_stage",
        "processing_outcome",
        "recovery_status",
        "attempted",
        "confirmed",
        "failed_at",
    )
    unique = {
        json.dumps(
            {
                key: item[key]
                for key in ("record_kind", *meaningful, "target_references")
                if key in item
            },
            sort_keys=True,
            default=str,
        ): item
        for item in selected
    }
    view["processing_duplicate_count"] = len(selected) - len(unique)
    endings = [
        item for item in unique.values() if item.get("processing_status") not in {None, "attempted"}
    ]
    outcomes = {item.get("processing_status") for item in endings}
    detailed = [item for item in endings if item.get("record_kind") == "processing_attempt"]
    details = {
        json.dumps(
            {key: item[key] for key in meaningful if key in item}, sort_keys=True, default=str
        )
        for item in detailed
    }
    conflict = conflict or len(outcomes) > 1 or len(details) > 1
    if conflict:
        view.update(
            processing_status="unknown", processing_outcome="unknown", processing_conflict=True
        )
        return view
    chosen = detailed or endings or list(unique.values())
    for key in meaningful:
        values = [item[key] for item in chosen if key in item]
        if values:
            distinct = {json.dumps(value, sort_keys=True, default=str) for value in values}
            if len(distinct) == 1:
                view[key] = values[0]
    for key in ("mapping_status", "mapping_outcome", "recovery_status"):
        status_values = {item[key] for item in selected if isinstance(item.get(key), str)}
        if key == "recovery_status" and status_values - {"attempted"}:
            # Start and final records describe the same recovery, rather
            # than competing endings. Distinct final outcomes still conflict.
            status_values.discard("attempted")
        if len(status_values) == 1:
            view[key] = next(iter(status_values))
        elif len(status_values) > 1:
            view[key] = "unknown"
            view["processing_conflict"] = True
    references = [
        entry
        for item in selected
        for entry in item.get("target_references", [])
        if isinstance(entry, dict)
    ]
    if references:
        view["target_references"] = list(
            {json.dumps(entry, sort_keys=True): entry for entry in references}.values()
        )
    return view


def proposed_public_keys() -> tuple[str, ...]:
    """Metadata public keys D proposes for the root public layer."""
    return (
        "mapping_status",
        "mapping_outcome",
        "processing_status",
        "processing_stage",
        "processing_outcome",
        "recovery_status",
        "target_references",
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
