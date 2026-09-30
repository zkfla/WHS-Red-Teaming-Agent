"""Provided, observed, hypothesis and finding separation for LLM context.

The raw graph mixes declared scope, measured observations, plan-promoted
flags and open questions in one store. Consumers that plan the next move
must not read them as one flat list: a scope note is not an observation,
a plan flag promoted by tool success alone is not a measurement, and a
hypothesis that happens to appear in a generic listing is not a fact.

This module is the single read path that enforces that split. It only
reads through the existing graph schema (no new DB, no new store).
Return shape (engine and LLM consumers share this projection, never the
raw graph): provided and hypotheses and findings stay grouped dicts;
facts is a list where every item carries node_id, id and fact_ref plus
the granular fact_refs list; fact_refs is the merged measured-only
reference list; known_targets is a list where every item carries node_id
and id. Excluded plan-promoted flags are counted at the top level and
size-cut identifiers are reported as omitted lists.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.attack_graph.identity import finding_node_id, hypothesis_node_id
from redteam.attack_graph.predicates import attrs_of, facts_of, provenance_of
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.enums import GraphNodeType

SCOPE_LABEL = "engagement-scope"

PLAN_PROMOTED_ATTR_KEYS = frozenset({"reachable", "probed", "web_inspected", "mobile_analyzed"})

EXCLUDED_FACT_KEYS = frozenset({
    "attempted_operations",
    "evidence_ids",
    "last_operation",
    "last_operation_succeeded",
    "last_tool_succeeded",
    "last_verification_failed",
    "last_operation_blocked",
    "last_block_verdict",
    "last_block_reasons",
    "last_block_severity",
    "expected_observation",
    "reason",
    "hypothesis",
})

USABLE_TARGET_TYPES = frozenset({
    GraphNodeType.HOST,
    GraphNodeType.SERVICE,
    GraphNodeType.ENDPOINT,
    GraphNodeType.RESOURCE,
    GraphNodeType.ASSET,
    GraphNodeType.PRINCIPAL,
    GraphNodeType.CREDENTIAL,
})

MAX_PROVIDED_ITEMS = 50
MAX_FACT_ITEMS = 100
MAX_HYPOTHESIS_ITEMS = 50
MAX_FINDING_ITEMS = 50
MAX_TARGET_ITEMS = 100
MAX_OBSERVATIONS_PER_NODE = 10
MAX_EVIDENCE_IDS_PER_ITEM = 20
class ProvidedClaim(BaseModel):
    node_id: str = ""
    node_type: str = ""
    key: str = ""
    target_ref: str | None = None
    auth_session_refs: list[str] = Field(default_factory=list)
    origin: str = "declared"
    source: str = ""
    confidence: str = "server_declared"
    verification: str = "unconfirmed"
    in_scope: bool = True
    first_seen_step: int = 0
    last_seen_step: int = 0


class FactItem(BaseModel):
    node_id: str = ""
    node_type: str = ""
    observed_attrs: dict[str, Any] = Field(default_factory=dict)
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    first_seen_step: int = 0
    last_seen_step: int = 0
    origin: str = "observed"
class ExcludedAttr(BaseModel):
    node_id: str = ""
    attr: str = ""
    value: Any = None
    reason: str = ""


class HypothesisItem(BaseModel):
    hypothesis_id: str = ""
    claim: str = ""
    confidence: float = 0.0
    status: str = "open"
    target_node_id: str | None = None
    target: str | None = None
    agent_id: str | None = None
    step_id: str | None = None
    violation_if_confirmed: bool | None = None
    severity: str | None = None
    expectation: dict[str, Any] | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    first_seen_step: int | None = None
    last_seen_step: int | None = None
    origin: str = "inferred"


class FindingItem(BaseModel):
    finding_id: str = ""
    title: str = ""
    description: str = ""
    severity: str = "info"
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    is_grounded: bool = True
    origin: str = "observed"
    first_seen_step: int | None = None
    last_seen_step: int | None = None


class KnownTarget(BaseModel):
    node_id: str = ""
    node_type: str = ""
    address: str = ""
    target_ref: str | None = None
    observed_target: str | None = None
    origin: str = "observed"
    verification: str = "unconfirmed"
    last_seen_step: int = 0


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted([_jsonable(v) for v in value], key=str)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        try:
            return _jsonable(value.value)
        except Exception:
            pass
    return str(value)


def _sorted_str(values: Any) -> list[str]:
    seen: list[str] = []
    for v in values or []:
        s = str(v)
        if s not in seen:
            seen.append(s)
    return sorted(seen)


def _node_key(node: Any) -> str:
    raw = str(getattr(node, "id", ""))
    _, sep, key = raw.partition(":")
    return key or raw


def _steps_of(properties: dict[str, Any]) -> tuple[int, int]:
    first = properties.get("first_seen_step", 0)
    last = properties.get("last_seen_step", 0)
    try:
        first_i = int(first) if isinstance(first, int) else 0
    except Exception:
        first_i = 0
    try:
        last_i = int(last) if isinstance(last, int) else 0
    except Exception:
        last_i = 0
    return first_i, last_i


def _provenance_summary(properties: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in provenance_of(properties):
        if not isinstance(entry, dict):
            continue
        out.append({
            "source": str(entry.get("source", "")),
            "confidence": str(entry.get("confidence", "")),
            "step": int(entry.get("step", 0)) if isinstance(entry.get("step"), int) else 0,
            "run_id": str(entry.get("run_id")) if entry.get("run_id") is not None else None,
            "evidence_id": str(entry.get("evidence_id")) if entry.get("evidence_id") is not None else None,
        })
    return out


def _has_measured(properties: dict[str, Any], run_id: str | None = None) -> bool:
    for entry in provenance_of(properties):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("confidence", "")) != "measured":
            continue
        if run_id is None:
            return True
        entry_run = entry.get("run_id")
        if entry_run is None or str(entry_run) == str(run_id):
            return True
    return False
class HypothesisService:
    """Dedicated hypothesis read/write path over the existing graph schema.

    All reads go through list_hypotheses / get_hypothesis on the graph
    service. Generic node listings are never used here, so a hypothesis
    that leaks into a broad query cannot be mistaken for a fact. No new
    database or store is created; hypothesis nodes and TARGETS /
    SUPPORTED_BY edges defined by the attack graph schema are reused.
    """

    def __init__(self, graph_service: Any) -> None:
        self._graph = graph_service

    async def list(self, status: str | None = None) -> list[Hypothesis]:
        func = getattr(self._graph, "list_hypotheses", None)
        if not callable(func):
            raise TypeError("graph service has no list_hypotheses; hypotheses need the dedicated lookup path")
        items = await func(status=status) if status is not None else await func()
        return sorted(list(items or []), key=lambda h: str(h.hypothesis_id))

    async def get(self, hypothesis_id: str) -> Hypothesis | None:
        func = getattr(self._graph, "get_hypothesis", None)
        if not callable(func):
            raise TypeError("graph service has no get_hypothesis; hypotheses need the dedicated lookup path")
        return await func(str(hypothesis_id))

    async def record(self, hypothesis: Hypothesis, about: Any = None, step: int = 0, provenance: Any = None) -> Any:
        func = getattr(self._graph, "record_hypothesis", None)
        if not callable(func):
            raise TypeError("graph service has no record_hypothesis")
        kwargs: dict[str, Any] = {"step": step}
        if about is not None:
            kwargs["about"] = about
        if provenance is not None:
            kwargs["provenance"] = provenance
        return await func(hypothesis, **kwargs)

    async def resolve(self, hypothesis_id: str, status: str, confidence: float | None = None, step: int = 0) -> Any:
        func = getattr(self._graph, "resolve_hypothesis", None)
        if not callable(func):
            raise TypeError("graph service has no resolve_hypothesis")
        kwargs: dict[str, Any] = {"status": status, "step": step}
        if confidence is not None:
            kwargs["confidence"] = confidence
        return await func(str(hypothesis_id), **kwargs)

    async def relations_for(self, hypothesis_id: str) -> list[dict[str, Any]]:
        node_id = str(hypothesis_node_id(str(hypothesis_id)))
        try:
            edges = await self._graph.list_edges()
        except Exception:
            return []
        rels: list[dict[str, Any]] = []
        for edge in edges or []:
            if str(edge.source_id) == node_id or str(edge.target_id) == node_id:
                rels.append({
                    "edge_type": str(edge.type.value if hasattr(edge.type, "value") else edge.type),
                    "source_id": str(edge.source_id),
                    "target_id": str(edge.target_id),
                })
        rels.sort(key=lambda d: (d["edge_type"], d["source_id"], d["target_id"]))
        return rels

    async def evidence_ids_for(self, hypothesis_id: str, evidence_store: Any, run_id: str) -> list[str]:
        hid = str(hypothesis_id)
        ids: list[str] = []
        hyp = await self.get(hid)
        if hyp is not None:
            meta = hyp.metadata or {}
            for key in ("evidence_ids", "supporting_evidence_ids", "refuting_evidence_ids"):
                raw = meta.get(key, [])
                if isinstance(raw, list):
                    ids.extend([str(v) for v in raw])
        try:
            node = await self._graph.get_node(hypothesis_node_id(hid))
        except Exception:
            node = None
        if node is not None:
            ids.extend(_node_evidence_ids(node))
        try:
            records = await evidence_store.list_for_run(run_id)
        except Exception:
            records = []
        for rec in records or []:
            if str(getattr(rec, "hypothesis_id", "") or "") == hid:
                ids.append(str(rec.evidence_id))
        return _sorted_str(ids)


def _provided_claim(node: Any) -> ProvidedClaim:
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    first, last = _steps_of(props)
    prov = _provenance_summary(props)
    source = str(prov[0]["source"]) if prov else ""
    sessions: list[str] = []
    auth_cfg = facts.get("auth_session_config", {})
    if isinstance(auth_cfg, dict):
        raw_sessions = auth_cfg.get("sessions", [])
        if isinstance(raw_sessions, list):
            for item in raw_sessions:
                if isinstance(item, dict) and item.get("session_ref") is not None:
                    sessions.append(str(item.get("session_ref")))
                elif isinstance(item, str):
                    sessions.append(item)
    target_ref = facts.get("target_ref", None)
    measured = _has_measured(props)
    observed = bool(_node_observations(node)) or bool(_node_evidence_ids(node))
    verification = "confirmed" if (measured and observed) else "unconfirmed"
    return ProvidedClaim(
        node_id=str(node.id),
        node_type=str(node.type.value if hasattr(node.type, "value") else node.type),
        key=_node_key(node),
        target_ref=str(target_ref) if isinstance(target_ref, str) else None,
        auth_session_refs=sorted(set(sessions)),
        origin="declared",
        source=source,
        confidence="server_declared",
        verification=verification,
        in_scope=bool(attrs_of(props).get("in_scope", False)),
        first_seen_step=first,
        last_seen_step=last,
    )



def _node_evidence_ids(node: Any) -> list[str]:
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    ids: list[str] = []
    raw = facts.get("evidence_ids", [])
    if isinstance(raw, list):
        ids.extend([str(v) for v in raw])
    for entry in provenance_of(props):
        if isinstance(entry, dict) and entry.get("evidence_id") is not None:
            ids.append(str(entry.get("evidence_id")))
    return _sorted_str(ids)


def _node_observations(node: Any) -> list[dict[str, Any]]:
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    raw = facts.get("observations", [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("context_key"), str):
            continue
        out.append(_jsonable(item))
    out.sort(key=lambda d: str(d.get("context_key", "")))
    return out[:MAX_OBSERVATIONS_PER_NODE]


def _is_provided_node(node: Any) -> bool:
    labels = list(getattr(node, "labels", []) or [])
    if SCOPE_LABEL in labels:
        return True
    props = getattr(node, "properties", {}) or {}
    attrs = attrs_of(props)
    if attrs.get("in_scope") is True:
        for entry in provenance_of(props):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("confidence", "")) == "server_declared":
                return True
            if str(entry.get("source", "")).startswith("engagement:"):
                return True
    return False
def _fact_view(node: Any, run_id: str) -> tuple:
    node_type = getattr(node, "type", None)
    if node_type in (GraphNodeType.HYPOTHESIS, GraphNodeType.FINDING):
        return None, []
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    attrs = attrs_of(props)
    first, last = _steps_of(props)
    measured = _has_measured(props, run_id) or _has_measured(props, None)
    observations = _node_observations(node)
    evidence_ids = _node_evidence_ids(node)[:MAX_EVIDENCE_IDS_PER_ITEM]
    has_support = bool(observations) or bool(evidence_ids)
    excluded: list[ExcludedAttr] = []
    kept_attrs: dict[str, Any] = {}
    for key in sorted(attrs, key=str):
        value = _jsonable(attrs[key])
        if key in PLAN_PROMOTED_ATTR_KEYS and not (measured and has_support):
            excluded.append(ExcludedAttr(node_id=str(node.id), attr=str(key), value=value, reason="plan_promoted_without_observation"))
            continue
        if not measured:
            excluded.append(ExcludedAttr(node_id=str(node.id), attr=str(key), value=value, reason="no_measured_provenance"))
            continue
        kept_attrs[str(key)] = value
    kept_facts: dict[str, Any] = {}
    if measured:
        for key in sorted(facts, key=str):
            if key in EXCLUDED_FACT_KEYS:
                continue
            if key == "observations":
                continue
            kept_facts[str(key)] = _jsonable(facts[key])
    if not kept_attrs and not kept_facts and not observations:
        return None, excluded
    item = FactItem(
        node_id=str(node.id),
        node_type=str(node_type.value if hasattr(node_type, "value") else node_type),
        observed_attrs=kept_attrs,
        observed_facts=kept_facts,
        observations=observations,
        evidence_ids=evidence_ids,
        provenance=_provenance_summary(props),
        first_seen_step=first,
        last_seen_step=last,
        origin="observed",
    )
    return item, excluded


def _priority(last_seen: int, node_id: str, targets: set[str], step: int) -> tuple:
    in_target = 0 if node_id in targets else 1
    future = 0 if last_seen <= step else 1
    return (in_target, future, -last_seen, node_id)
async def _hypothesis_items(graph_service: Any, evidence_store: Any, run_id: str, targets: set[str], step: int) -> list:
    svc = HypothesisService(graph_service)
    try:
        hyps = await svc.list()
    except TypeError:
        raise
    except Exception:
        hyps = []
    try:
        edges = await graph_service.list_edges()
    except Exception:
        edges = []
    edge_list = list(edges or [])
    try:
        records = await evidence_store.list_for_run(run_id)
    except Exception:
        records = []
    by_hyp: dict[str, list[str]] = {}
    for rec in records or []:
        hid = str(getattr(rec, "hypothesis_id", "") or "")
        if hid:
            by_hyp.setdefault(hid, []).append(str(rec.evidence_id))
    finding_links: dict[str, list[str]] = {}
    target_links: dict[str, list[str]] = {}
    for edge in edge_list:
        etype = str(edge.type.value if hasattr(edge.type, "value") else edge.type)
        src = str(edge.source_id)
        tgt = str(edge.target_id)
        if etype == "supported_by" and tgt.startswith("hypothesis:"):
            hid = tgt.partition(":")[2]
            finding_links.setdefault(hid, []).append(src)
        if etype == "targets" and src.startswith("hypothesis:"):
            hid = src.partition(":")[2]
            target_links.setdefault(hid, []).append(tgt)
    items: list[HypothesisItem] = []
    for hyp in hyps:
        hid = str(hyp.hypothesis_id)
        meta = hyp.metadata or {}
        node = None
        try:
            node = await graph_service.get_node(hypothesis_node_id(hid))
        except Exception:
            node = None
        first = last = None
        if node is not None:
            f, l = _steps_of(getattr(node, "properties", {}) or {})
            first, last = f, l
        node_evidence: list[str] = []
        if node is not None:
            node_evidence.extend(_node_evidence_ids(node))
        meta_evidence: list[str] = []
        for key in ("evidence_ids", "supporting_evidence_ids", "refuting_evidence_ids"):
            raw = meta.get(key, [])
            if isinstance(raw, list):
                meta_evidence.extend([str(v) for v in raw])
        linked = _sorted_str(node_evidence + meta_evidence + by_hyp.get(hid, []))[:MAX_EVIDENCE_IDS_PER_ITEM]
        status = str(hyp.status or "open")
        if status in ("rejected", "inconclusive"):
            supporting: list[str] = []
            refuting = list(linked)
        else:
            supporting = list(linked)
            refuting = []
        meta_target = meta.get("target_node_id", None)
        edge_targets = sorted(set(target_links.get(hid, [])))
        target_node_id = str(meta_target) if isinstance(meta_target, str) and meta_target else (edge_targets[0] if edge_targets else None)
        related = _sorted_str(edge_targets + finding_links.get(hid, []))
        rels: list[dict[str, Any]] = []
        hnode = str(hypothesis_node_id(hid))
        for edge in edge_list:
            if str(edge.source_id) == hnode or str(edge.target_id) == hnode:
                rels.append({
                    "edge_type": str(edge.type.value if hasattr(edge.type, "value") else edge.type),
                    "source_id": str(edge.source_id),
                    "target_id": str(edge.target_id),
                })
        rels.sort(key=lambda d: (d["edge_type"], d["source_id"], d["target_id"]))
        needs: list[str] = []
        if status == "open":
            if target_node_id:
                needs.append("observe " + target_node_id + " with a tool that records evidence for this claim")
            else:
                needs.append("pin the claim to one concrete target node before further validation")
            exp = meta.get("expectation", None)
            if isinstance(exp, dict) and exp.get("description"):
                needs.append("check expectation: " + str(exp.get("description"))[:240])
            else:
                needs.append("record supporting or refuting evidence with the hypothesis id attached")
        expectation = _jsonable(meta.get("expectation")) if meta.get("expectation") is not None else None
        if expectation is not None and not isinstance(expectation, dict):
            expectation = {"value": expectation}
        items.append(HypothesisItem(
            hypothesis_id=hid,
            claim=str(hyp.description or ""),
            confidence=float(hyp.confidence or 0.0),
            status=status,
            target_node_id=target_node_id,
            target=str(meta.get("target")) if meta.get("target") is not None else None,
            agent_id=str(meta.get("agent_id")) if meta.get("agent_id") is not None else None,
            step_id=str(meta.get("step_id")) if meta.get("step_id") is not None else None,
            violation_if_confirmed=bool(meta.get("violation_if_confirmed")) if meta.get("violation_if_confirmed") is not None else None,
            severity=str(meta.get("severity")) if meta.get("severity") is not None else None,
            expectation=expectation,
            supporting_evidence_ids=supporting,
            refuting_evidence_ids=refuting,
            evidence_ids=linked,
            finding_ids=_sorted_str([s.partition(":")[2] if s.startswith("finding:") else s for s in finding_links.get(hid, [])]),
            related_node_ids=related,
            relations=rels,
            needs=needs,
            first_seen_step=first,
            last_seen_step=last,
            origin="inferred",
        ))
    def _key(item: HypothesisItem) -> tuple:
        pri = 0 if (item.target_node_id in targets) else 1
        last_seen = item.last_seen_step if isinstance(item.last_seen_step, int) else -1
        future = 0 if last_seen <= step else 1
        return (pri, future, -last_seen, item.hypothesis_id)
    items.sort(key=_key)
    return items
async def _finding_items(graph_service: Any, run_id: str, targets: set[str], step: int) -> list:
    try:
        findings = await graph_service.list_findings()
    except Exception:
        findings = []
    try:
        edges = await graph_service.list_edges()
    except Exception:
        edges = []
    edge_list = list(edges or [])
    hyp_links: dict[str, list[str]] = {}
    tgt_links: dict[str, list[str]] = {}
    for edge in edge_list:
        etype = str(edge.type.value if hasattr(edge.type, "value") else edge.type)
        src = str(edge.source_id)
        tgt = str(edge.target_id)
        if etype == "supported_by" and src.startswith("finding:"):
            fid = src.partition(":")[2]
            hyp_links.setdefault(fid, []).append(tgt.partition(":")[2] if tgt.startswith("hypothesis:") else tgt)
        if etype == "targets" and src.startswith("finding:"):
            fid = src.partition(":")[2]
            tgt_links.setdefault(fid, []).append(tgt)
    items: list[FindingItem] = []
    for f in findings or []:
        fid = str(f.finding_id)
        node = None
        try:
            node = await graph_service.get_node(finding_node_id(fid))
        except Exception:
            node = None
        first = last = None
        if node is not None:
            a, b = _steps_of(getattr(node, "properties", {}) or {})
            first, last = a, b
        ev = _sorted_str(list(f.evidence_ids or []))[:MAX_EVIDENCE_IDS_PER_ITEM]
        grounded = bool(ev) if bool(f.verified) else True
        items.append(FindingItem(
            finding_id=fid,
            title=str(f.title or ""),
            description=str(f.description or ""),
            severity=str(f.severity or "info"),
            verified=bool(f.verified),
            evidence_ids=ev,
            hypothesis_ids=_sorted_str(hyp_links.get(fid, [])),
            target_node_ids=_sorted_str(tgt_links.get(fid, [])),
            is_grounded=grounded,
            origin="observed" if ev else "inferred",
            first_seen_step=first,
            last_seen_step=last,
        ))
    def _key(item: FindingItem) -> tuple:
        hit = 0 if (targets and any(t in targets for t in item.target_node_ids)) else 1
        last_seen = item.last_seen_step if isinstance(item.last_seen_step, int) else -1
        future = 0 if last_seen <= step else 1
        return (hit, future, -last_seen, item.finding_id)
    items.sort(key=_key)
    return items


def _target_address(node: Any) -> tuple[str | None, str | None, str | None]:
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    target_ref = facts.get("target_ref", None)
    observed = facts.get("observed_target", None)
    if not isinstance(target_ref, str):
        target_ref = None
    if not isinstance(observed, str):
        observed = None
    key = _node_key(node)
    ntype = getattr(node, "type", None)
    address = observed or target_ref or key
    if ntype == GraphNodeType.ENDPOINT:
        method = facts.get("method", None)
        path = facts.get("path", None) or observed
        host = facts.get("host", None)
        parts: list[str] = []
        if isinstance(host, str) and host:
            parts.append(host)
        if isinstance(method, str) and isinstance(path, str):
            parts.append(str(method) + " " + str(path))
        if parts:
            address = " ".join(parts)
    return address, target_ref, observed


async def _known_targets(nodes: list, run_id: str, targets: set[str], step: int) -> list:
    out: list[KnownTarget] = []
    for node in nodes:
        ntype = getattr(node, "type", None)
        if ntype not in USABLE_TARGET_TYPES:
            continue
        props = getattr(node, "properties", {}) or {}
        provided = _is_provided_node(node)
        measured = _has_measured(props, run_id) or _has_measured(props, None)
        observations = _node_observations(node)
        evidence_ids = _node_evidence_ids(node)
        if provided:
            origin = "declared"
            verification = "confirmed" if (measured and (observations or evidence_ids)) else "unconfirmed"
        else:
            if not measured:
                continue
            if not (observations or evidence_ids or facts_of(props).get("target_ref") or facts_of(props).get("observed_target")):
                if ntype in (GraphNodeType.PRINCIPAL, GraphNodeType.CREDENTIAL):
                    pass
                else:
                    continue
            origin = "observed"
            verification = "confirmed" if (observations or evidence_ids) else "unconfirmed"
            if verification != "confirmed":
                continue
        address, target_ref, observed = _target_address(node)
        if not address:
            continue
        f, l = _steps_of(props)
        out.append(KnownTarget(
            node_id=str(node.id),
            node_type=str(ntype.value if hasattr(ntype, "value") else ntype),
            address=str(address),
            target_ref=target_ref,
            observed_target=observed,
            origin=origin,
            verification=verification,
            last_seen_step=l,
        ))
    out.sort(key=lambda t: _priority(t.last_seen_step, t.node_id, targets, step))
    return out


def _fact_refs_for_item(item: FactItem) -> list[str]:
    refs: list[str] = [str(item.node_id)]
    for key in (item.observed_attrs or {}):
        refs.append(str(item.node_id) + "#attr:" + str(key))
    for key in (item.observed_facts or {}):
        refs.append(str(item.node_id) + "#fact:" + str(key))
    for obs in (item.observations or []):
        if isinstance(obs, dict) and obs.get("context_key"):
            refs.append(str(item.node_id) + "#obs:" + str(obs.get("context_key")))
    for eid in (item.evidence_ids or []):
        refs.append("evidence:" + str(eid))
    out: list[str] = []
    for r in refs:
        if r not in out:
            out.append(r)
    return sorted(out)

async def project_knowledge(graph_service: Any, evidence_store: Any, run_id: str, step: int = 0, target_node_ids: list[str] | None = None) -> dict:
    run = str(run_id)
    targets: set[str] = set([str(v) for v in (target_node_ids or [])])
    try:
        nodes = await graph_service.list_nodes()
    except Exception:
        nodes = []
    node_list = list(nodes or [])
    provided_all: list[ProvidedClaim] = []
    for node in sorted(node_list, key=lambda n: str(n.id)):
        if _is_provided_node(node):
            provided_all.append(_provided_claim(node))
    provided_all.sort(key=lambda c: _priority(c.last_seen_step, c.node_id, targets, step))
    provided_ids = [c.node_id for c in provided_all]
    provided_kept, provided_omitted = _truncate_ids(provided_ids, MAX_PROVIDED_ITEMS)
    provided_map = {c.node_id: c for c in provided_all}
    provided = [provided_map[i].model_dump(mode="json") for i in provided_kept]

    fact_all: list[FactItem] = []
    excluded_all: list[ExcludedAttr] = []
    for node in sorted(node_list, key=lambda n: str(n.id)):
        item, exc = _fact_view(node, run)
        excluded_all.extend(exc)
        if item is not None:
            fact_all.append(item)
    fact_all.sort(key=lambda f: _priority(f.last_seen_step, f.node_id, targets, step))
    fact_ids = [f.node_id for f in fact_all]
    fact_kept, fact_omitted = _truncate_ids(fact_ids, MAX_FACT_ITEMS)
    fact_map = {f.node_id: f for f in fact_all}
    fact_dicts: list[dict] = []
    all_fact_refs: list[str] = []
    for i in fact_kept:
        item = fact_map[i]
        refs = _fact_refs_for_item(item)
        all_fact_refs.extend(refs)
        d = item.model_dump(mode="json")
        d["node_id"] = item.node_id
        d["id"] = item.node_id
        d["fact_ref"] = item.node_id
        d["fact_refs"] = refs
        fact_dicts.append(d)
    fact_refs = _sorted_str(all_fact_refs)
    excluded_all.sort(key=lambda e: (e.node_id, e.attr))

    hyps = await _hypothesis_items(graph_service, evidence_store, run, targets, step)
    hyp_ids = [h.hypothesis_id for h in hyps]
    hyp_kept, hyp_omitted = _truncate_ids(hyp_ids, MAX_HYPOTHESIS_ITEMS)
    hyp_map = {h.hypothesis_id: h for h in hyps}
    hypotheses = [hyp_map[i].model_dump(mode="json") for i in hyp_kept]

    finds = await _finding_items(graph_service, run, targets, step)
    find_ids = [f.finding_id for f in finds]
    find_kept, find_omitted = _truncate_ids(find_ids, MAX_FINDING_ITEMS)
    find_map = {f.finding_id: f for f in finds}
    findings = [find_map[i].model_dump(mode="json") for i in find_kept]

    known_all = await _known_targets(node_list, run, targets, step)
    known_ids = [t.node_id for t in known_all]
    known_kept, known_omitted = _truncate_ids(known_ids, MAX_TARGET_ITEMS)
    known_map = {t.node_id: t for t in known_all}
    known_targets: list[dict] = []
    for i in known_kept:
        t = known_map[i].model_dump(mode="json")
        t["node_id"] = str(known_map[i].node_id)
        t["id"] = str(known_map[i].node_id)
        known_targets.append(t)

    return {
        "provided": {"claims": provided, "omitted_node_ids": provided_omitted},
        "facts": fact_dicts,
        "fact_refs": fact_refs,
        "hypotheses": {"items": hypotheses, "omitted_hypothesis_ids": hyp_omitted},
        "findings": {"items": findings, "omitted_finding_ids": find_omitted},
        "known_targets": known_targets,
        "excluded_inferred_attr_count": len(excluded_all),
        "excluded_inferred_attrs": [e.model_dump(mode="json") for e in excluded_all],
        "omitted_fact_node_ids": fact_omitted,
        "omitted_known_target_ids": known_omitted,
    }


def _truncate_ids(sorted_ids: list[str], limit: int) -> tuple[list[str], list[str]]:
    if limit <= 0:
        return [], list(sorted_ids)
    return list(sorted_ids[:limit]), list(sorted_ids[limit:])


__all__ = [
    "HypothesisService",
    "ProvidedClaim",
    "FactItem",
    "ExcludedAttr",
    "HypothesisItem",
    "FindingItem",
    "KnownTarget",
    "PLAN_PROMOTED_ATTR_KEYS",
    "project_knowledge",
]







