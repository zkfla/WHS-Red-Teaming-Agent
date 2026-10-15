"""Common worker graph-recording handler (B1). Registered as runtime_tool.

The model submits normalized facts/hypotheses/findings/branches plus
evidence ids only. It never repeats action/invocation/tool ids, and no
top-level raw pointer is required when per-fact pointers suffice.
The runtime resolves authoritative origins from the evidence ids and
fills the ids itself.

This handler performs the domain writes itself through the existing
AttackGraphService: measured facts land on existing nodes (or edges),
or declare a new typed entity whose id is code-derived and linked back
to the observed source, hypotheses are recorded as INFERRED, and a
first vulnerability claim
runs posthoc review, a final-admission receipt, and promotion here.
Standalone hypotheses are never reviewed here; they stay open inference
until an explicit finding or execution verification resolves them.
The root generic runtime_tool dispatch only returns this handler's
result; it never branches on the graph name or performs hidden
follow-up registration.

``raw_path`` addresses the preserved ``ToolResult.output``: the stored
raw payload is the full ToolResult dict, and a pointer may either list
keys under that output or start with the envelope key ``output``, which
is consumed once (both forms are equivalent). Segments are dict keys or
list indices, written as decimal text or as an integer; an empty path
addresses the whole output. Exact pointers are code-checked for
observations only; inference and findings stay free semantic claims
with evidence refs.

Original proposals and stored metadata are read-only here. First-claim
review never depends on an already-confirmed graph status (no cycle):
review runs first, resolution follows. Unresolved items and branches
are registered into the existing stores and their refs returned; no
parallel graph store is created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import (
    CLAIM_ATTR_PREFIX,
    CLAIM_FACT_PREFIX,
    RESERVED_KEYS,
    Confidence,
    Provenance,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import EndpointTarget, HostTarget, TargetRef, UrlTarget
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType, GraphEdgeType
from redteam.core.extensions import Contribution
from redteam.core.ids import EngagementId, EvidenceId, RunId, new_id
from redteam.orchestration.analysis import (
    _posthoc_source_gap,
    append_posthoc_claim_receipt,
    claim_target_is_grounded,
    hypothesis_status_for_assessment,
)
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.diagnostics import DecisionDiagnostic, diagnostic_for_exception
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.findings import FindingPromoter, PromotionRequest
from redteam.orchestration.local_tools import (
    LocalToolContext,
    LocalToolDescriptor,
    LocalToolResult,
    runtime_tool_contribution,
)
from redteam.orchestration.observations.shell_execution import is_shell_execution_envelope

GRAPH_RECORD_TOOL_ID = "graph.record"
GRAPH_RECORD_CONTRIBUTION_ID = "graph-record"


class FactEdge(BaseModel):
    """Inferred relation between existing nodes; the scalar stays measured on its source."""

    model_config = ConfigDict(extra="forbid")

    edge_type: str = Field(min_length=1)
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)


class NewEntityDecl(BaseModel):
    # Typed discovered-entity declaration. The node id is code-derived
    # from target via the common identity, and creation always travels
    # with the source relation record below. Whether the producer
    # evidence shows actual contact or mere discovery is computed by
    # code from the executed target, never chosen by the model.
    model_config = ConfigDict(extra="forbid")

    target: TargetRef
    relation: str = Field(
        default="observed",
        description=(
            "Optional inferred relation from source to entity. Code always "
            "records the address-backed observed relation separately."
        ),
    )


#: Envelope key the preserved ToolResult keeps its payload under. The walk
#: consumes it once, so quoting it as the leading segment stays equivalent.
_OUTPUT_ENVELOPE_KEY = "output"

#: Single source of the model-visible pointer rule; the field descriptions
#: and the tool descriptor reuse it so the published contract cannot drift.
_RAW_PATH_RULE = (
    "The stored raw payload is the full ToolResult dict, and the pointer "
    "addresses its preserved output: a segment is a dict key or a list "
    "index, written as decimal text or as an integer. The output-relative "
    "form lists keys under that output; the equivalent envelope-prefixed "
    "form may start with the envelope key 'output', which is consumed once "
    "(['output'] addresses the whole output, as does an empty path)."
)


class RecordedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    raw_path: list[StrictStr | StrictInt] = Field(description=_RAW_PATH_RULE)
    value: Any = None
    fact_name: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    edge: FactEdge | None = None
    new_entity: NewEntityDecl | None = Field(default=None)
    entity_raw_path: list[StrictStr | StrictInt] | None = Field(
        default=None,
        min_length=1,
        description=(
            _RAW_PATH_RULE
            + " Declares the new entity identity; defaults to raw_path. A "
            "new different entity is refused without an address string or "
            "typed target value at this pointer."
        ),
    )


class HypothesisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        min_length=1,
        description=(
            "Open inference only: registering a question here never confirms, "
            "refutes, or reviews it. Resolution needs an explicit finding "
            "or execution verification."
        ),
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Optional same-run evidence for the question; emptiness alone "
            "does not refuse the draft."
        ),
    )
    target_node_id: str | None = Field(
        default=None,
        description=(
            "Optional target linkage; omission keeps the draft open and "
            "unresolved rather than refused."
        ),
    )


class FindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    target_node_id: str | None = None
    hypothesis_id: str | None = None
    severity: str = "medium"
    principal_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Accounts this vulnerability claim is about, if the claim "
            "names any. Optional: observations without an asserted account "
            "must not invent one. Each named account needs actual-use proof."
        ),
    )


class BranchNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: str = ""


class GraphRecordingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[RecordedFact] = Field(default_factory=list)
    hypotheses: list[HypothesisDraft] = Field(default_factory=list)
    findings: list[FindingDraft] = Field(default_factory=list)
    branches: list[BranchNote] = Field(default_factory=list)


def _locate(raw: Any, path: list[str | int]) -> tuple[bool, Any]:
    """Walk dict keys / list indices. Never raises for a bad path."""
    node = raw
    for part in path:
        if isinstance(node, dict):
            if not isinstance(part, str):
                part = str(part)
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, (list, tuple)):
            if not isinstance(part, int):
                try:
                    part = int(part)
                except (TypeError, ValueError):
                    return False, None
            try:
                node = node[part]
            except (IndexError, TypeError):
                return False, None
        else:
            return False, None
    return True, node


def _walk_output(output: Any, path: list[str | int]) -> tuple[bool, Any]:
    """Locate one pointer under the preserved output.

    The pointer is tried exactly as sent. Only when it does not resolve and
    it starts with the envelope key ``output`` is that single leading
    segment dropped once, so the output-relative form and the
    envelope-prefixed form address the same stored location. A pointer that
    already resolves is never re-pointed, so a value mismatch at the sent
    location stays a mismatch.
    """
    found, actual = _locate(output, path)
    if found:
        return found, actual
    if path and path[0] == _OUTPUT_ENVELOPE_KEY:
        return _locate(output, list(path[1:]))
    return False, None


def _remember(refs: list[str], value: Any) -> None:
    """Collect one cited evidence id. Names only, never values."""
    name = str(value or "").strip()
    if name and name not in refs:
        refs.append(name)


def _refused_entry(
    base: dict[str, Any], reason: str, id_key: str = "hypothesis_id"
) -> dict[str, Any]:
    """One masked refusal entry. Reason is a fixed token, never a value."""
    return {**base, id_key: None, "status": "refused", "reason": reason}


def _is_reserved_fact_name(name: Any) -> bool:
    """Reuse the graph reserved boundary for verification/claim plumbing.

    A recorded fact is a semantic measurement identical to its source
    path value only. Plumbing keys (reserved words, per-claim prefixes)
    are refused here so a raw success flag can never be rewritten as a
    measured security verdict. Originals are never rewritten; new meaning
    travels by receipt and promotion only.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return True
    if cleaned in RESERVED_KEYS:
        return True
    return cleaned.startswith(CLAIM_ATTR_PREFIX) or cleaned.startswith(CLAIM_FACT_PREFIX)


def _measured_provenance(*, step: int, evidence_id: str | None, run_id: str) -> Provenance:
    return Provenance(
        step=step,
        source="local:graph.record",
        evidence_id=evidence_id,
        run_id=run_id,
        confidence=Confidence.MEASURED,
    )


def _inferred_provenance(*, step: int, evidence_id: str | None, run_id: str) -> Provenance:
    return Provenance(
        step=step,
        source="local:graph.record",
        evidence_id=evidence_id,
        run_id=run_id,
        confidence=Confidence.INFERRED,
    )


_TARGET_REF_ADAPTER: TypeAdapter[TargetRef] = TypeAdapter(TargetRef)


def _proposal_target(metadata: Any) -> Any | None:
    try:
        proposal = metadata.get("proposal") if isinstance(metadata, dict) else None
    except Exception:
        return None
    if not isinstance(proposal, dict):
        return None
    target_dict = proposal.get("target")
    if not isinstance(target_dict, dict):
        return None
    try:
        return _TARGET_REF_ADAPTER.validate_python(target_dict)
    except Exception:
        return None


def _address_candidate_ids(value: str, *, base_url: str | None) -> set[str]:
    text = value.strip()
    if not text:
        return set()
    ids: set[str] = set()
    try:
        ids.add(str(node_id_for_target(HostTarget(host=text))))
    except Exception:
        pass
    try:
        ids.add(str(node_id_for_target(UrlTarget(url=text))))
    except Exception:
        pass
    def _add_split(raw_url: str) -> None:
        try:
            parts = urlsplit(raw_url if "://" in raw_url else "//" + raw_url)
        except Exception:
            return
        host = parts.hostname
        if not host:
            return
        try:
            port = parts.port
        except ValueError:
            return
        try:
            ids.add(
                str(
                    node_id_for_target(
                        EndpointTarget(host=host, port=port, path=parts.path or "/")
                    )
                )
            )
        except Exception:
            pass
    _add_split(text)
    if base_url and text.startswith("/"):
        try:
            absolute = urljoin(base_url, text)
        except Exception:
            absolute = ""
        if absolute:
            try:
                ids.add(str(node_id_for_target(UrlTarget(url=absolute))))
            except Exception:
                pass
            _add_split(absolute)
    return ids


class GraphRecordingHandler:
    """Ground and write one recording call. No independent budget.

    Facts are code-checked against the preserved raw output and written
    to existing nodes (or edges), or declare a new typed entity whose
    id is code-derived and linked back to the observed source with a
    code-computed discovery label. A new different entity additionally
    needs its identity shown in the cited output at the entity pointer,
    otherwise it is refused and belongs in a hypothesis instead.
    Hypotheses are recorded as the
    model's inference. Findings run posthoc review, a final-admission
    receipt, and promotion here. Review never requires an
    already-confirmed graph status; it runs first, resolution follows.
    """

    def __init__(
        self,
        *,
        evidence_store: Any = None,
        graph_service: Any = None,
        verifier: Any = None,
    ) -> None:
        self._store = evidence_store
        self._graph = graph_service
        self._verifier = verifier

    async def _sources_same_run(
        self, store: Any, refs: list[str], run_id: str, engagement_id: str
    ) -> tuple[bool, str]:
        for ref in refs:
            try:
                record = await store.get(ref)
            except Exception:
                return False, "store unreadable"
            if record is None:
                return False, "evidence missing"
            if str(getattr(record, "run_id", "")) != run_id:
                return False, "other run"
            if engagement_id and str(getattr(record, "engagement_id", "") or "") != engagement_id:
                return False, "other run"
        return True, ""

    async def _write_fact(
        self,
        fact: RecordedFact,
        *,
        store: Any,
        service: Any,
        run_id: str,
        engagement_id: str,
        step: int,
        accepted_facts: list[dict[str, Any]],
        refused_facts: list[dict[str, Any]],
        record_ids: list[str],
    ) -> None:
        eid = fact.evidence_id.strip()
        target = fact.target_node_id.strip()
        if not target:
            refused_facts.append({"evidence_id": eid, "reason": "no target linkage"})
            return
        if _is_reserved_fact_name(fact.fact_name):
            refused_facts.append({"evidence_id": eid, "reason": "reserved fact name"})
            return
        try:
            record = await store.get(eid)
        except Exception:
            refused_facts.append({"evidence_id": eid, "reason": "store unreadable"})
            return
        if record is None:
            refused_facts.append({"evidence_id": eid, "reason": "evidence missing"})
            return
        if getattr(record, "source_type", None) is not EvidenceSourceType.TOOL:
            refused_facts.append({"evidence_id": eid, "reason": "not tool evidence"})
            return
        if str(getattr(record, "run_id", "")) != run_id:
            refused_facts.append({"evidence_id": eid, "reason": "other run"})
            return
        if engagement_id and str(getattr(record, "engagement_id", "") or "") != engagement_id:
            refused_facts.append({"evidence_id": eid, "reason": "other run"})
            return
        raw = raw_from_metadata(getattr(record, "metadata", None))
        if not isinstance(raw, dict):
            refused_facts.append({"evidence_id": eid, "reason": "no preserved raw"})
            return
        gap = _posthoc_source_gap(record, raw)
        if gap is not None:
            refused_facts.append({"evidence_id": eid, "reason": gap})
            return
        output = raw.get(_OUTPUT_ENVELOPE_KEY)
        if not isinstance(output, (dict, str)):
            refused_facts.append({"evidence_id": eid, "reason": "no preserved output"})
            return
        found, actual = _walk_output(output, fact.raw_path)
        if not found:
            refused_facts.append({"evidence_id": eid, "reason": "raw path missing"})
            return
        if actual != fact.value:
            refused_facts.append({"evidence_id": eid, "reason": "value mismatch"})
            return
        metadata = getattr(record, "metadata", None)
        observed_target = metadata.get("target_node_id") if isinstance(metadata, dict) else None
        if not isinstance(observed_target, str) or not observed_target.strip():
            refused_facts.append({"evidence_id": eid, "reason": "evidence names no target"})
            return
        if observed_target.strip() != target:
            refused_facts.append({"evidence_id": eid, "reason": "observed another target"})
            return
        if is_shell_execution_envelope(output):
            refused_facts.append({
                "evidence_id": eid,
                "reason": "shell output is a program assertion, not a measured remote fact",
            })
            return
        if service is None:
            refused_facts.append({"evidence_id": eid, "reason": "graph unavailable"})
            return
        provenance = _measured_provenance(step=step, evidence_id=eid, run_id=run_id)
        if fact.edge is not None and fact.new_entity is not None:
            refused_facts.append({"evidence_id": eid, "reason": "ambiguous relation"})
            return
        if fact.new_entity is not None:
            await self._write_new_entity_fact(
                fact,
                eid=eid,
                target=target,
                metadata=metadata,
                output=output,
                service=service,
                step=step,
                provenance=provenance,
                accepted_facts=accepted_facts,
                refused_facts=refused_facts,
                record_ids=record_ids,
            )
            return
        try:
            if fact.edge is not None:
                try:
                    edge_type = GraphEdgeType(str(fact.edge.edge_type).strip())
                except ValueError:
                    refused_facts.append({"evidence_id": eid, "reason": "unknown edge type"})
                    return
                source_id = fact.edge.source_node_id.strip()
                edge_target = fact.edge.target_node_id.strip()
                if target not in (source_id, edge_target):
                    refused_facts.append(
                        {"evidence_id": eid, "reason": "edge misses observed target"}
                    )
                    return
                source_node = await service.get_node(source_id)
                edge_node = await service.get_node(edge_target)
                if source_node is None or edge_node is None:
                    refused_facts.append(
                        {"evidence_id": eid, "reason": "edge endpoint missing"}
                    )
                    return
                # Exact scalar equality grounds only the source's attribute;
                # it does not prove the model-selected relation or endpoint.
                measured = await service.upsert_node(
                    source_node.type if str(source_node.id) == target else edge_node.type,
                    node_id=target,
                    facts={fact.fact_name: fact.value},
                    provenance=provenance,
                    step=step,
                )
                edge = await service.link(
                    edge_type,
                    source_node.id,
                    edge_node.id,
                    properties={
                        fact.fact_name: fact.value,
                        "relation": {
                            "type": edge_type.value,
                            "source_node_id": str(source_node.id),
                            "target_node_id": str(edge_node.id),
                        },
                    },
                    provenance=_inferred_provenance(step=step, evidence_id=eid, run_id=run_id),
                )
                record_ids.append(str(measured.id))
                record_ids.append(str(edge.id))
                accepted_facts.append(
                    {
                        "evidence_id": eid,
                        "target_node_id": target,
                        "fact_name": fact.fact_name,
                        "record_id": str(edge.id),
                        "measurement_record_id": str(measured.id),
                        "relation_confidence": Confidence.INFERRED.value,
                    }
                )
            else:
                node = await service.get_node(target)
                if node is None:
                    refused_facts.append({"evidence_id": eid, "reason": "unknown target node"})
                    return
                updated = await service.upsert_node(
                    node.type,
                    node_id=node.id,
                    facts={fact.fact_name: fact.value},
                    provenance=provenance,
                    step=step,
                )
                record_ids.append(str(updated.id))
                accepted_facts.append(
                    {
                        "evidence_id": eid,
                        "target_node_id": target,
                        "fact_name": fact.fact_name,
                        "record_id": str(updated.id),
                    }
                )
        except Exception:
            refused_facts.append({"evidence_id": eid, "reason": "graph write failed"})

    async def _write_new_entity_fact(
        self,
        fact: Any,
        *,
        eid: str,
        target: str,
        metadata: Any,
        output: Any,
        service: Any,
        step: int,
        provenance: Any,
        accepted_facts: list,
        refused_facts: list,
        record_ids: list,
    ) -> None:
        decl = fact.new_entity
        new_target = decl.target
        if getattr(new_target, "kind", "") == "graph_node":
            refused_facts.append({"evidence_id": eid, "reason": "arbitrary node id"})
            return
        try:
            new_node_id = str(node_id_for_target(new_target))
        except Exception:
            refused_facts.append({"evidence_id": eid, "reason": "cannot identify target"})
            return
        try:
            relation_raw = str(getattr(decl, "relation", "observed") or "observed")
            relation = GraphEdgeType(relation_raw.strip())
        except ValueError:
            refused_facts.append({"evidence_id": eid, "reason": "unknown edge type"})
            return
        proposal_target = _proposal_target(metadata)
        executed_node_id = target
        if proposal_target is not None:
            try:
                executed_node_id = str(node_id_for_target(proposal_target))
            except Exception:
                pass
        observation = "confirmed" if executed_node_id == new_node_id else "discovered"
        if new_node_id != executed_node_id:
            entity_path = fact.entity_raw_path if fact.entity_raw_path else fact.raw_path
            entity_found, entity_value = _walk_output(output, entity_path)
            if not entity_found:
                refused_facts.append(
                    {"evidence_id": eid, "reason": "entity identity not shown"}
                )
                return
            if isinstance(entity_value, str):
                base_url = None
                try:
                    if getattr(proposal_target, "kind", "") == "url":
                        candidate_base = getattr(proposal_target, "url", "")
                        if isinstance(candidate_base, str) and candidate_base.strip():
                            base_url = candidate_base.strip()
                except Exception:
                    base_url = None
                if new_node_id not in _address_candidate_ids(
                    entity_value, base_url=base_url
                ):
                    refused_facts.append(
                        {"evidence_id": eid, "reason": "entity identity not shown"}
                    )
                    return
            elif isinstance(entity_value, dict):
                try:
                    typed_id = str(
                        node_id_for_target(_TARGET_REF_ADAPTER.validate_python(entity_value))
                    )
                except Exception:
                    typed_id = ""
                if typed_id != new_node_id:
                    refused_facts.append(
                        {"evidence_id": eid, "reason": "entity identity not shown"}
                    )
                    return
            else:
                refused_facts.append(
                    {"evidence_id": eid, "reason": "entity identity not shown"}
                )
                return
        if service is None:
            refused_facts.append({"evidence_id": eid, "reason": "graph unavailable"})
            return
        try:
            anchor = await service.get_node(target)
        except Exception:
            anchor = None
        if anchor is None:
            refused_facts.append({"evidence_id": eid, "reason": "unknown target node"})
            return
        try:
            node = await service.observe_target(
                new_target,
                facts={fact.fact_name: fact.value},
                attrs={"observation": observation, "discovery_evidence_id": eid},
                provenance=provenance,
                step=step,
            )
        except Exception:
            refused_facts.append({"evidence_id": eid, "reason": "graph write failed"})
            return
        if str(node.id) == str(anchor.id):
            record_ids.append(str(node.id))
            accepted_facts.append(
                {
                    "evidence_id": eid,
                    "target_node_id": str(node.id),
                    "fact_name": fact.fact_name,
                    "record_id": str(node.id),
                    "observation": observation,
                }
            )
            return
        try:
            edge = await service.link(
                GraphEdgeType.OBSERVED,
                anchor.id,
                node.id,
                properties={
                    "observation": observation,
                    "relation": {
                        "type": GraphEdgeType.OBSERVED.value,
                        "source_node_id": str(anchor.id),
                        "target_node_id": str(node.id),
                    },
                },
                provenance=provenance,
            )
        except Exception:
            refused_facts.append({"evidence_id": eid, "reason": "graph write failed"})
            return
        record_ids.append(str(node.id))
        record_ids.append(str(edge.id))
        inferred_edge_id = None
        if relation is not GraphEdgeType.OBSERVED:
            try:
                inferred_edge = await service.link(
                    relation,
                    anchor.id,
                    node.id,
                    properties={
                        "relation": {
                            "type": relation.value,
                            "source_node_id": str(anchor.id),
                            "target_node_id": str(node.id),
                        },
                    },
                    provenance=provenance.model_copy(update={"confidence": Confidence.INFERRED}),
                )
                inferred_edge_id = str(inferred_edge.id)
                record_ids.append(inferred_edge_id)
            except Exception:
                refused_facts.append(
                    {"evidence_id": eid, "reason": "inferred relation write failed"}
                )
        accepted_facts.append(
            {
                "evidence_id": eid,
                "target_node_id": str(node.id),
                "fact_name": fact.fact_name,
                "record_id": str(node.id),
                "edge_id": str(edge.id),
                "inferred_edge_id": inferred_edge_id,
                "observation": observation,
            }
        )

    async def _write_hypothesis(
        self,
        draft: HypothesisDraft,
        *,
        store: Any,
        service: Any,
        run_id: str,
        engagement_id: str,
        step: int,
        new_hypothesis_ids: list[str],
    ) -> dict[str, Any]:
        refs = [str(item or "").strip() for item in draft.evidence_refs]
        refs = [item for item in refs if item]
        base: dict[str, Any] = {"statement": draft.statement[:500]}
        if service is None:
            return _refused_entry(base, "graph unavailable")
        ok, reason = await self._sources_same_run(store, refs, run_id, engagement_id)
        if not ok:
            return _refused_entry(base, reason)
        about: str | None = None
        if draft.target_node_id is not None and draft.target_node_id.strip():
            about = draft.target_node_id.strip()
            try:
                node = await service.get_node(about)
            except Exception:
                node = None
            if node is None:
                return _refused_entry(base, "unknown target node")
        elif refs:
            source_targets = set()
            for ref in refs:
                source = await store.get(ref)
                source_targets.add(str(source.metadata.get("target_node_id", "") or ""))
            if len(source_targets) != 1 or not next(iter(source_targets)):
                return _refused_entry(base, "hypothesis spans targets")
            about = next(iter(source_targets))
        normalized = " ".join(draft.statement.strip().split())[:2000]
        stable_id = "hyp_" + content_hash(
            {"run": run_id, "target": about or "", "statement": normalized}
        )[:12]
        try:
            existing = await service.get_hypothesis(stable_id)
        except Exception:
            existing = None
        if existing is not None:
            if stable_id not in new_hypothesis_ids:
                new_hypothesis_ids.append(stable_id)
            return {
                **base,
                "hypothesis_id": stable_id,
                "status": str(getattr(existing, "status", "") or "open"),
                "target_node_id": about,
                "reused": True,
            }
        hypothesis = Hypothesis(
            hypothesis_id=stable_id,
            description=draft.statement.strip()[:2000],
            confidence=0.0,
            status="open",
            metadata={
                "origin": "graph.record",
                "evidence_refs": refs,
                **({"target_node_id": about} if about else {}),
            },
        )
        try:
            await service.record_hypothesis(
                hypothesis,
                about=about,
                step=step,
                provenance=_inferred_provenance(
                    step=step, evidence_id=refs[0] if refs else None, run_id=run_id
                ),
            )
        except Exception:
            return _refused_entry(base, "graph write failed")
        new_hypothesis_ids.append(hypothesis.hypothesis_id)
        return {
            **base,
            "hypothesis_id": hypothesis.hypothesis_id,
            "status": "open",
            "target_node_id": about,
        }

    async def __call__(
        self, tool_input: dict[str, Any], context: LocalToolContext
    ) -> LocalToolResult:
        try:
            recording = GraphRecordingInput.model_validate(tool_input or {})
        except Exception as exc:
            # Same shared validation contract as the native-loop helpers:
            # this input's declared schema publishes the field and rule,
            # never the raw values or undeclared key names, so the next
            # turn can correct the shape instead of retrying it blind.
            diagnostic = diagnostic_for_exception(
                exc,
                stage="native_local_tool",
                schema=GraphRecordingInput.model_json_schema(),
            )
            return LocalToolResult(
                ok=False,
                error=diagnostic.feedback(),
                payload={"diagnostic": diagnostic.model_dump(mode="json")},
            )
        store = self._store
        if store is None:
            store = getattr(getattr(context, "deps", None), "evidence_store", None)
        runtime = getattr(context, "runtime", None)
        run_id = str(getattr(runtime, "run_id", "") or "").strip()
        engagement_id = str(getattr(runtime, "engagement_id", "") or "").strip()
        if store is None or not run_id:
            return LocalToolResult(ok=False, error="recording needs store and run")
        state = getattr(context.deps, "central_state", None)
        active_task_id = getattr(runtime, "active_task_id", None)
        task = (await state.get_task(str(active_task_id))
                if state is not None and active_task_id else None)
        if task is not None and task.probe_limits is not None and (
            recording.hypotheses or any(
                item.hypothesis_id not in task.hypothesis_revisions for item in recording.findings
            )
        ):
            diagnostic = DecisionDiagnostic(
                code="probe_candidate_change_requires_planner", stage="native_local_tool",
                kind="validation", retryable=False,
                summary="Return new candidate proposals as feedback. Only admitted candidates "
                        "may use claim review during this probe.",
            )
            return LocalToolResult(ok=False, error=diagnostic.feedback(),
                                   payload={"diagnostic": diagnostic.model_dump(mode="json")})
        service = self._graph
        if service is None:
            graph_store = getattr(getattr(context, "deps", None), "graph_store", None)
            if graph_store is not None:
                try:
                    service = AttackGraphService(graph_store)
                except Exception:
                    service = None
        try:
            step = int(getattr(runtime, "step_count", 0) or 0)
        except Exception:
            step = 0
        seen_refs: list[str] = []
        new_evidence_ids: list[str] = []
        record_ids: list[str] = []
        accepted_facts: list[dict[str, Any]] = []
        refused_facts: list[dict[str, Any]] = []
        for fact in recording.facts:
            _remember(seen_refs, fact.evidence_id)
            await self._write_fact(
                fact,
                store=store,
                service=service,
                run_id=run_id,
                engagement_id=engagement_id,
                step=step,
                accepted_facts=accepted_facts,
                refused_facts=refused_facts,
                record_ids=record_ids,
            )
        hypotheses_out: list[dict[str, Any]] = []
        new_hypothesis_ids: list[str] = []
        for draft in recording.hypotheses:
            for ref in draft.evidence_refs:
                _remember(seen_refs, ref)
            hypotheses_out.append(
                await self._write_hypothesis(
                    draft,
                    store=store,
                    service=service,
                    run_id=run_id,
                    engagement_id=engagement_id,
                    step=step,
                    new_hypothesis_ids=new_hypothesis_ids,
                )
            )
        review: dict[str, Any] = {"status": "per-claim"}
        finding_verifier = self._verifier or getattr(context.deps, "verifier", None)
        # Standalone hypotheses stay open here: recording a question is not
        # resolving it. An explicit finding below reviews and resolves its own
        # hypothesis; other hypotheses resolve through execution verification.
        findings_out: list[dict[str, Any]] = []
        existing_claims: set[str] = set()
        if recording.findings and service is not None:
            try:
                for item in await service.list_findings():
                    key = (getattr(item, "metadata", None) or {}).get("claim_key")
                    if key:
                        existing_claims.add(str(key))
            except Exception:
                existing_claims = set()
        for finding_draft in recording.findings:
            for ref in finding_draft.evidence_refs:
                _remember(seen_refs, ref)
            findings_out.append(
                await self._promote_finding(
                    finding_draft,
                    store=store,
                    service=service,
                    run_id=run_id,
                    engagement_id=engagement_id,
                    step=step,
                    verifier=finding_verifier,
                    budget=context.budget,
                    before_call=context.before_call,
                    new_hypothesis_ids=new_hypothesis_ids,
                    existing_claims=existing_claims,
                    new_evidence_ids=new_evidence_ids,
                    record_ids=record_ids,
                )
            )
        for entry in hypotheses_out:
            if entry.get("hypothesis_id"):
                current = await service.get_hypothesis(entry["hypothesis_id"])
                if current is not None and entry.get("status") != "refused":
                    entry["status"] = current.status
        branches_out: list[dict[str, Any]] = []
        for note in recording.branches:
            for ref in note.evidence_refs:
                _remember(seen_refs, ref)
            branches_out.append(
                await self._write_branch(
                    note,
                    store=store,
                    run_id=run_id,
                    engagement_id=engagement_id,
                    new_evidence_ids=new_evidence_ids,
                )
            )
        refused_any = bool(refused_facts) or any(
            entry.get("status") == "refused"
            for entry in (*hypotheses_out, *findings_out, *branches_out)
        )
        call_id = "local_" + content_hash(
            {"run": run_id, "recording": recording.model_dump(mode="json")}
        )[:24]
        payload = {
            "accepted_facts": accepted_facts,
            "refused_facts": refused_facts,
            "hypotheses": hypotheses_out,
            "findings": findings_out,
            "branches": branches_out,
            "claim_review": review,
            "record_ids": record_ids,
        }
        return LocalToolResult(
            ok=not refused_any,
            payload=payload,
            refs=tuple([*seen_refs, *new_evidence_ids]),
            error=None if not refused_any else "some items refused; see payload",
            local_call_id=call_id,
        )


    async def _promote_finding(
        self,
        draft: FindingDraft,
        *,
        store: Any,
        service: Any,
        run_id: str,
        engagement_id: str,
        step: int,
        verifier: Any,
        budget: Any,
        before_call: Any,
        new_hypothesis_ids: list[str],
        existing_claims: set[str],
        new_evidence_ids: list[str],
        record_ids: list[str],
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"statement": draft.statement[:500]}
        refs = [str(item or "").strip() for item in draft.evidence_refs]
        refs = [item for item in refs if item]
        if service is None:
            return _refused_entry(base, "graph unavailable", "finding_id")
        if not refs:
            return _refused_entry(base, "no evidence reference", "finding_id")
        ok, reason = await self._sources_same_run(store, refs, run_id, engagement_id)
        if not ok:
            return _refused_entry(base, reason, "finding_id")
        try:
            first = await store.get(refs[0])
        except Exception:
            first = None
        if first is None:
            return _refused_entry(base, "evidence missing", "finding_id")
        first_meta = getattr(first, "metadata", None)
        if not isinstance(first_meta, dict):
            first_meta = {}
        action_id = getattr(first, "action_id", None)
        invocation_id = getattr(first, "invocation_id", None)
        if action_id is None or invocation_id is None:
            return {**base, "finding_id": None, "status": "refused", "reason": "origin unlinkable"}
        target: str | None = None
        if draft.target_node_id is not None and draft.target_node_id.strip():
            target = draft.target_node_id.strip()
        else:
            seen_targets: set[str] = set()
            for ref in refs:
                try:
                    candidate = await store.get(ref)
                except Exception:
                    candidate = None
                candidate_meta = (
                    getattr(candidate, "metadata", None) if candidate is not None else None
                )
                value = (
                    candidate_meta.get("target_node_id")
                    if isinstance(candidate_meta, dict)
                    else None
                )
                if isinstance(value, str) and value.strip():
                    seen_targets.add(value.strip())
            if len(seen_targets) != 1:
                return _refused_entry(base, "finding spans targets", "finding_id")
            target = next(iter(seen_targets))
        try:
            target_node = await service.get_node(target)
        except Exception:
            target_node = None
        if target_node is None:
            return _refused_entry(base, "unknown target node", "finding_id")
        hypothesis_id: str | None = None
        if draft.hypothesis_id is not None and draft.hypothesis_id.strip():
            hypothesis_id = draft.hypothesis_id.strip()
        else:
            for candidate_id in new_hypothesis_ids:
                candidate = await service.get_hypothesis(candidate_id)
                if candidate is not None and (
                    candidate.metadata.get("target_node_id") == target
                    and (
                        len(new_hypothesis_ids) == 1
                        or " ".join(candidate.description.split())
                        == " ".join(draft.statement.split())
                    )
                ):
                    hypothesis_id = candidate_id
                    break
        if hypothesis_id is None:
            # Finding-only first claim: code builds the hyp context from
            # the claim itself; the model is never forced to file one first.
            created = await self._write_hypothesis(
                HypothesisDraft(
                    statement=draft.statement,
                    evidence_refs=refs,
                    target_node_id=target,
                ),
                store=store,
                service=service,
                run_id=run_id,
                engagement_id=engagement_id,
                step=step,
                new_hypothesis_ids=[],
            )
            if created.get("status") == "refused" or not created.get("hypothesis_id"):
                return _refused_entry(
                    base, str(created.get("reason") or "hypothesis unwritable"), "finding_id"
                )
            hypothesis_id = str(created["hypothesis_id"])
        stored_hypothesis = await service.get_hypothesis(hypothesis_id)
        if stored_hypothesis is None or (
            (stored_hypothesis.contract_version == 1
             and stored_hypothesis.metadata.get("target_node_id") != target)
            or (stored_hypothesis.contract_version == 2
                and target not in stored_hypothesis.target_node_ids)
        ):
            return _refused_entry(base, "hypothesis claim or target differs", "finding_id")
        reviewed_hypothesis = stored_hypothesis.model_copy(deep=True)
        for ref in refs:
            source = await store.get(ref)
            if _posthoc_source_gap(source, raw_from_metadata(source.metadata)) is not None:
                return _refused_entry(base, "source origin invalid", "finding_id")
            if not await claim_target_is_grounded(service, source, target, evidence_store=store):
                return _refused_entry(
                    base, "claim target has no source-backed relation", "finding_id"
                )
        verify = getattr(verifier, "verify_claim_with_budget", None)
        if verify is None or budget is None:
            return {
                **base,
                "hypothesis_id": hypothesis_id,
                "target_node_id": target,
                "finding_id": None,
                "status": "refused",
                "reason": "claim not reviewed",
            }
        claimed_principals: list[str] = []
        for _p in draft.principal_refs or []:
            _clean = str(_p or "").strip()
            if _clean and _clean not in claimed_principals:
                claimed_principals.append(_clean)
            if len(claimed_principals) >= 8:
                break
        try:
            assessment = await verify(
                claim={
                    "statement": draft.statement[:2000],
                    "target_node_id": target,
                    "hypothesis_id": hypothesis_id,
                    "principal_refs": list(claimed_principals),
                    "reference_hypothesis": {
                        "hypothesis_id": hypothesis_id,
                        "description": stored_hypothesis.description,
                        **({"premise": reviewed_hypothesis.premise,
                            "target_node_ids": reviewed_hypothesis.target_node_ids,
                            "principal_refs": reviewed_hypothesis.principal_refs,
                            "candidate_revision": reviewed_hypothesis.revision}
                           if reviewed_hypothesis.contract_version == 2 else {}),
                    },
                },
                evidence_ids=list(refs),
                run_id=run_id,
                budget=budget,
                before_call=before_call,
                security_claim=True,
            )
        except Exception:
            return {
                **base,
                "hypothesis_id": hypothesis_id,
                "target_node_id": target,
                "finding_id": None,
                "status": "refused",
                "reason": "review failed",
            }
        finding_verdict = getattr(assessment, "verdict", "?")
        review_summary: dict[str, Any] = {
            "verdict": (
                finding_verdict.value
                if hasattr(finding_verdict, "value")
                else str(finding_verdict)
            ),
            "outcome": getattr(assessment, "outcome", None),
            "reason": str(getattr(assessment, "reason", "") or "")[:300],
        }
        try:
            stored_hypothesis = await service.get_hypothesis(hypothesis_id)
        except Exception:
            stored_hypothesis = None
        hypothesis_status = str(getattr(stored_hypothesis, "status", "") or "")
        try:
            receipt = await append_posthoc_claim_receipt(
                evidence_store=store,
                run_id=run_id,
                engagement_id=engagement_id,
                claim={
                    "statement": draft.statement,
                    "target_node_id": target,
                    "principal_refs": list(claimed_principals),
                },
                evidence_ids=refs,
                assessment=assessment,
                hypothesis_id=hypothesis_id,
                action_id=str(action_id),
                invocation_id=str(invocation_id),
                target_node_id=target,
                candidate=reviewed_hypothesis,
            )
        except Exception:
            receipt = None
        if receipt is None:
            return {
                **base,
                "hypothesis_id": hypothesis_id,
                "target_node_id": target,
                "finding_id": None,
                "status": "refused",
                "reason": "receipt unwritable",
                "review": review_summary,
            }
        new_evidence_ids.append(str(receipt.evidence_id))
        if reviewed_hypothesis.contract_version == 2:
            return {
                **base, "hypothesis_id": hypothesis_id, "target_node_id": target,
                "receipt_id": str(receipt.evidence_id), "finding_id": None,
                "status": "reviewed", "reason": "candidate judgment belongs to the planner",
                "review": review_summary,
            }
        # Only this matching claim is resolved, after its receipt is durable.
        try:
            if assessment.hypothesis_outcome not in ("success", "refuted"):
                return {
                    **base, "hypothesis_id": hypothesis_id, "target_node_id": target,
                    "receipt_id": str(receipt.evidence_id), "finding_id": None,
                    "status": "refused", "reason": "reference hypothesis unresolved",
                    "review": review_summary,
                }
            hyp_assessment = assessment.model_copy(
                update={"outcome": assessment.hypothesis_outcome}
            )
            hypothesis_status = hypothesis_status_for_assessment(hyp_assessment)
            updated = await service.resolve_hypothesis(
                hypothesis_id, status=hypothesis_status, step=step
            )
            if updated is None:
                raise ValueError("hypothesis missing")
        except Exception:
            return _refused_entry(base, "hypothesis resolution failed", "finding_id")
        if assessment.outcome != "success" or assessment.claim_supported is not True:
            return {
                **base, "hypothesis_id": hypothesis_id, "target_node_id": target,
                "receipt_id": str(receipt.evidence_id), "finding_id": None,
                "status": "refused", "reason": "claim not supported", "review": review_summary,
            }

        capability = first_meta.get("capability") or "graph.record"
        operation = first_meta.get("operation") or "record"
        agent = first_meta.get("agent_id") or ""
        promotion = FindingPromoter().promote(
            PromotionRequest(
                verdict=assessment.verdict,
                reason=str(getattr(assessment, "reason", "") or "")[:500],
                evidence_ids=list(refs),
                hypothesis_id=hypothesis_id,
                hypothesis_status=hypothesis_status,
                target_node_id=target,
                capability=str(capability),
                operation=str(operation),
                agent_id=str(agent),
                action_id=str(action_id),
                reviewer_id=str(getattr(assessment, "reviewer_id", "") or ""),
                source_refs=list(refs),
                severity=str(draft.severity or "medium"),
                assessment_kind="security_check",
                extra_metadata={
                    "outcome": assessment.outcome,
                    "claim_supported": assessment.claim_supported,
                    "review_receipt_id": str(receipt.evidence_id),
                },
            ),
            existing_claims=existing_claims,
        )
        if promotion.finding is None:
            return {
                **base,
                "hypothesis_id": hypothesis_id,
                "target_node_id": target,
                "receipt_id": str(receipt.evidence_id),
                "finding_id": None,
                "status": "refused",
                "reason": "promotion refused: " + str(promotion.refused_reason),
                "review": review_summary,
            }
        try:
            node = await service.record_finding(
                promotion.finding,
                hypothesis_id=hypothesis_id,
                about=target,
                step=step,
                provenance=_measured_provenance(step=step, evidence_id=refs[0], run_id=run_id),
            )
        except Exception:
            return {
                **base,
                "hypothesis_id": hypothesis_id,
                "target_node_id": target,
                "receipt_id": str(receipt.evidence_id),
                "finding_id": None,
                "status": "refused",
                "reason": "graph write failed",
            }
        record_ids.append(str(node.id))
        try:
            _claim_key = (promotion.finding.metadata or {}).get("claim_key")
        except Exception:
            _claim_key = None
        if _claim_key:
            existing_claims.add(str(_claim_key))
        return {
            **base,
            "hypothesis_id": hypothesis_id,
            "target_node_id": target,
            "receipt_id": str(receipt.evidence_id),
            "finding_id": str(promotion.finding.finding_id),
            "status": "recorded",
            "review": review_summary,
        }

    async def _write_branch(
        self,
        note: BranchNote,
        *,
        store: Any,
        run_id: str,
        engagement_id: str,
        new_evidence_ids: list[str],
    ) -> dict[str, Any]:
        refs = [str(item or "").strip() for item in note.evidence_refs]
        refs = [item for item in refs if item]
        ok, reason = await self._sources_same_run(store, refs, run_id, engagement_id)
        if not ok:
            return {
                "question": note.question[:300],
                "evidence_id": None,
                "status": "refused",
                "reason": reason,
            }
        payload: dict[str, Any] = {
            "record_kind": "branch_note",
            "origin": "graph.record",
            "question": note.question.strip()[:1000],
            "rationale": note.rationale.strip()[:1000] if note.rationale else "",
            "evidence_refs": refs,
        }
        record = EvidenceRecord(
            evidence_id=EvidenceId(new_id("ev")),
            run_id=RunId(run_id),
            engagement_id=EngagementId(engagement_id),
            source_type=EvidenceSourceType.AGENT,
            source_id="graph.record",
            action_id=None,
            invocation_id=None,
            hypothesis_id=None,
            content_type="application/json",
            artifact_ref=None,
            digest=digest_payload(payload),
            summary=("branch: " + note.question.strip())[:280],
            created_at=datetime.now(timezone.utc),
            metadata=payload,
        )
        try:
            await store.append(record)
        except Exception:
            return {
                "question": note.question[:300],
                "evidence_id": None,
                "status": "refused",
                "reason": "store unwritable",
            }
        new_evidence_ids.append(str(record.evidence_id))
        return {
            "question": note.question[:300],
            "evidence_id": str(record.evidence_id),
            "status": "recorded",
        }


def graph_recording_descriptor() -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=GRAPH_RECORD_TOOL_ID,
        description=(
            "Record normalized facts/hypotheses/findings/branches. Facts are "
            "code-checked against the preserved ToolResult output (raw_path: "
            + _RAW_PATH_RULE
            + ") and written to existing graph nodes or edges only; "
            "hypotheses stay open inference here and resolve through an "
            "explicit finding or execution verification; findings run "
            "posthoc review plus promotion here. A finding with a hypothesis_id and "
            "grounded retained evidence_refs can request an independent review receipt "
            "when a verifier and review budget are available. The receipt is bound to the "
            "candidate revision and purpose, without another target call; UNKNOWN remains "
            "unresolved. Task completion receipts are not candidate reviews. "
            "Shell streams and exit codes are program "
            "assertions/execution metadata; use evidence-linked hypotheses or unresolved "
            "observations, never measured remote facts."
        ),
        input_schema=GraphRecordingInput.model_json_schema(),
        contribution_id=GRAPH_RECORD_CONTRIBUTION_ID,
        version="4",
    )


def graph_recording_contribution() -> Contribution:
    """Pluggy declaration. Assembly wiring stays root-owned.

    The factory takes only the stores; the verifier is resolved per call
    from the invocation context so the canonical root verifier is reused
    instead of built twice.
    """

    def factory(wiring: Any) -> tuple[LocalToolDescriptor, GraphRecordingHandler]:
        store = getattr(wiring, "evidence_store", None)
        graph_store = getattr(wiring, "graph_store", None)
        service = None
        if graph_store is not None:
            try:
                service = AttackGraphService(graph_store)
            except Exception:
                service = None
        return graph_recording_descriptor(), GraphRecordingHandler(
            evidence_store=store, graph_service=service
        )


    return runtime_tool_contribution(
        contribution_id=GRAPH_RECORD_CONTRIBUTION_ID,
        version="4",
        factory=factory,
    )
