"""Provided, observed, hypothesis and finding separation for LLM context.

The raw graph mixes declared scope, measured observations, plan-promoted
flags and open questions in one store. Consumers that plan the next move
must not read them as one flat list: a scope note is not an observation,
a plan flag promoted by tool success alone is not a measurement, and a
hypothesis that happens to appear in a generic listing is not a fact.

This module is the single read path that enforces that split. It only
reads through the existing graph schema (no new DB, no new store).
The LLM receives a bounded projection; execution checks receive fresh,
uncapped internal assessment material from the same provenance rules.
In the projection, provided, hypotheses and findings stay grouped dicts;
facts is a list where every item carries node_id, id and fact_ref plus
the granular fact_refs list; fact_refs is the merged measured-only
reference list; known_targets is a list where every item carries node_id
and id. Excluded plan-promoted flags are counted at the top level and
size-cut identifiers are reported as omitted lists.
"""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.attack_graph.identity import finding_node_id, hypothesis_node_id
from redteam.attack_graph.predicates import (
    attrs_of,
    claim_provenance_of,
    digest_value,
    facts_of,
    provenance_of,
    values_equal,
)
from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import GraphNodeType
from redteam.orchestration.knowledge_validation import InternalAssessment, InternalTarget
from redteam.orchestration.progress import knowledge_fingerprint

SCOPE_LABEL = "engagement-scope"

PLAN_PROMOTED_ATTR_KEYS = frozenset({"reachable", "probed", "web_inspected", "mobile_analyzed"})

EXCLUDED_FACT_KEYS = frozenset(
    {
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
    }
)

#: Declared scope attributes are never measured facts. They stay in
#: ``provided`` no matter which provenance a later write attaches.
DECLARED_ATTR_KEYS = frozenset({"in_scope"})

#: Provided-only configuration. The declared participation config (which
#: sessions exist, display labels) stays in ``provided`` no matter what.
#: Actually returned binding values (``target_ref``, ``session_refs``,
#: ``session_ref``, ``credential_kind``) are deliberately NOT banned here:
#: the measured-source + same-run-raw + value-snapshot gate already blocks
#: declared promotion, so a session_list/compare response that really
#: returned them is a legitimate provisioned-binding fact (not an auth
#: success claim). Ungrounded declarations still never become facts.
DECLARED_FACT_KEYS = frozenset(
    {
        "auth_session_config",
        "auth_session_registrations",
        "display_name",
    }
)

#: Execution audit and mapper-failure bookkeeping. Kept on the node for
#: traceability, never projected as observed facts.
AUDIT_FACT_KEYS = frozenset(
    {
        "mapper_failure",
        "mapping_failure",
        "recovery_note",
        "recovery_result",
        "record_kind",
        "source_evidence_id",
        "authentication_checks",
        "authentication_outcome",
    }
)

#: Cap for per-id diagnostic lists so one noisy run cannot blow up the
#: projection payload.
MAX_DIAGNOSTIC_IDS = 200

USABLE_TARGET_TYPES = frozenset(
    {
        GraphNodeType.HOST,
        GraphNodeType.SERVICE,
        GraphNodeType.ENDPOINT,
        GraphNodeType.RESOURCE,
        GraphNodeType.ASSET,
        GraphNodeType.PRINCIPAL,
        GraphNodeType.CREDENTIAL,
    }
)

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
    grounded_evidence_ids: list[str] = Field(default_factory=list)
    claim_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)


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
    neutral_evidence_ids: list[str] = Field(default_factory=list)
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
        out.append(
            {
                "source": str(entry.get("source", "")),
                "confidence": str(entry.get("confidence", "")),
                "step": int(entry.get("step", 0)) if isinstance(entry.get("step"), int) else 0,
                "run_id": str(entry.get("run_id")) if entry.get("run_id") is not None else None,
                "evidence_id": str(entry.get("evidence_id"))
                if entry.get("evidence_id") is not None
                else None,
            }
        )
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


#: Evidence metadata keys holding the preserved raw tool payload.
_RAW_EVIDENCE_KEYS = ("tool_result", "raw_tool_result")


def _record_has_raw(record: Any) -> bool:
    """Whether an evidence record carries its raw tool payload."""
    meta = getattr(record, "metadata", {}) or {}
    if not isinstance(meta, dict):
        return False
    for key in _RAW_EVIDENCE_KEYS:
        if meta.get(key) is not None:
            return True
    return False


async def _build_evidence_index(
    evidence_store: Any, run_id: str, *, engagement_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Same-run evidence by id, plus explicit lookup diagnostics.

    A store failure never becomes an empty list: it marks ``lookup_failed``
    so every measured claim stays ungrounded with a reason instead of
    silently vanishing.
    """
    diagnostics: dict[str, Any] = {
        "lookup_failed": False,
        "lookup_error": None,
        "not_found": [],
        "no_raw": [],
        "run_mismatch": [],
        "truncated": [],
    }
    if evidence_store is None:
        diagnostics["lookup_failed"] = True
        diagnostics["lookup_error"] = "no_evidence_store"
        return {}, diagnostics
    try:
        records = await evidence_store.list_for_run(run_id)
    except Exception as exc:  # noqa: BLE001 - lookup failure is data, not a crash
        diagnostics["lookup_failed"] = True
        diagnostics["lookup_error"] = type(exc).__name__
        return {}, diagnostics
    by_id: dict[str, Any] = {}
    no_raw: list[str] = []
    for rec in records or []:
        eid = str(getattr(rec, "evidence_id", "") or "")
        if not eid:
            continue
        # list_for_run already scopes by run; verify each record anyway so
        # a mis-scoped store can never promote another run's evidence.
        rec_run = getattr(rec, "run_id", None)
        if rec_run is None or str(rec_run) != str(run_id):
            diagnostics["run_mismatch"].append(eid)
            continue
        if engagement_id is not None and str(getattr(rec, "engagement_id", "")) != engagement_id:
            diagnostics["run_mismatch"].append(eid)
            continue
        by_id[eid] = rec
        if not _record_has_raw(rec):
            no_raw.append(eid)
    diagnostics["no_raw"] = _sorted_str(no_raw)[:MAX_DIAGNOSTIC_IDS]
    return by_id, diagnostics


def _claim_grounding(
    entry: dict[str, Any] | None,
    *,
    run_id: str,
    evidence_index: dict[str, Any],
    index_failed: bool,
) -> tuple[bool, str, str | None]:
    """Grounding check for one claim-map entry.

    Returns ``(grounded, reason, evidence_id)``. Only a ``measured`` entry
    whose evidence resolves to a same-run record with a raw payload is
    grounded. ``historical`` means the claim belongs to another run and is
    reported separately, never merged into current facts.
    """
    if entry is None:
        return False, "legacy_no_claim", None
    confidence = str(entry.get("confidence", "") or "")
    if confidence != "measured":
        return False, "no_measured_provenance", None
    claim_run = entry.get("run_id")
    if claim_run is None:
        claim_run = entry.get("run")
    evidence_id = entry.get("evidence_id")
    if evidence_id is None:
        evidence_id = entry.get("evidence")
    if claim_run is None or str(claim_run) == "":
        return False, "run_missing", str(evidence_id) if evidence_id else None
    if str(claim_run) != str(run_id):
        return False, "historical", str(evidence_id) if evidence_id else None
    if evidence_id is None or str(evidence_id) == "":
        return False, "no_evidence", None
    eid = str(evidence_id)
    if index_failed:
        return False, "evidence_lookup_failed", eid
    rec = evidence_index.get(eid)
    if rec is None:
        return False, "evidence_not_found", eid
    if not _record_has_raw(rec):
        return False, "evidence_no_raw", eid
    return True, "grounded", eid


def _node_has_grounded_presence(
    props: dict[str, Any],
    *,
    run_id: str,
    evidence_index: dict[str, Any],
    index_failed: bool,
) -> bool:
    """Whether *any* claim on the node is grounded (presence, not promotion).

    Used only for provided-target verification: a target counts as observed
    once a tool actually ran against it with evidence, even though the
    recorded bookkeeping keys themselves never become facts.
    """
    if index_failed:
        return False
    for entry in claim_provenance_of(props).values():
        if not isinstance(entry, dict):
            continue
        elements = entry.get("elements")
        if isinstance(elements, list) and elements:
            for element in elements:
                if not isinstance(element, dict):
                    continue
                grounded, _, _ = _claim_grounding(
                    element, run_id=run_id, evidence_index=evidence_index, index_failed=index_failed
                )
                if grounded:
                    return True
            continue
        grounded, _, _ = _claim_grounding(
            entry, run_id=run_id, evidence_index=evidence_index, index_failed=index_failed
        )
        if grounded:
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
            raise TypeError(
                "graph service has no list_hypotheses; hypotheses need the dedicated lookup path"
            )
        items = await func(status=status) if status is not None else await func()
        return sorted(list(items or []), key=lambda h: str(h.hypothesis_id))

    async def get(self, hypothesis_id: str) -> Hypothesis | None:
        func = getattr(self._graph, "get_hypothesis", None)
        if not callable(func):
            raise TypeError(
                "graph service has no get_hypothesis; hypotheses need the dedicated lookup path"
            )
        return await func(str(hypothesis_id))

    async def record(
        self, hypothesis: Hypothesis, about: Any = None, step: int = 0, provenance: Any = None
    ) -> Any:
        func = getattr(self._graph, "record_hypothesis", None)
        if not callable(func):
            raise TypeError("graph service has no record_hypothesis")
        kwargs: dict[str, Any] = {"step": step}
        if about is not None:
            kwargs["about"] = about
        if provenance is not None:
            kwargs["provenance"] = provenance
        return await func(hypothesis, **kwargs)

    async def resolve(
        self, hypothesis_id: str, status: str, confidence: float | None = None, step: int = 0
    ) -> Any:
        func = getattr(self._graph, "resolve_hypothesis", None)
        if not callable(func):
            raise TypeError("graph service has no resolve_hypothesis")
        kwargs: dict[str, Any] = {"status": status, "step": step}
        if confidence is not None:
            kwargs["confidence"] = confidence
        return await func(str(hypothesis_id), **kwargs)

    async def relations_for(self, hypothesis_id: str) -> builtins.list[dict[str, Any]]:
        node_id = str(hypothesis_node_id(str(hypothesis_id)))
        try:
            edges = await self._graph.list_edges()
        except Exception:
            return []
        rels: list[dict[str, Any]] = []
        for edge in edges or []:
            if str(edge.source_id) == node_id or str(edge.target_id) == node_id:
                rels.append(
                    {
                        "edge_type": str(
                            edge.type.value if hasattr(edge.type, "value") else edge.type
                        ),
                        "source_id": str(edge.source_id),
                        "target_id": str(edge.target_id),
                    }
                )
        rels.sort(key=lambda d: (d["edge_type"], d["source_id"], d["target_id"]))
        return rels

    async def evidence_ids_for(
        self, hypothesis_id: str, evidence_store: Any, run_id: str
    ) -> builtins.list[str]:
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


def _provided_claim(
    node: Any,
    run_id: str = "",
    evidence_index: dict[str, Any] | None = None,
    index_failed: bool = False,
) -> ProvidedClaim:
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    first, last = _steps_of(props)
    prov = _provenance_summary(props)
    source = str(prov[0]["source"]) if prov else ""
    sessions = _declared_session_refs(props, run_id)
    target_ref = facts.get("target_ref", None)
    # Declared scope is never self-confirming: confirmed needs a grounded
    # measured write (an actual tool run with same-run raw evidence) behind
    # this node. Node-level flags and bare evidence-id lists do not count.
    if evidence_index is None:
        verification = "unconfirmed"
    else:
        observed = _node_has_grounded_presence(
            props, run_id=run_id, evidence_index=evidence_index, index_failed=index_failed
        )
        verification = "confirmed" if observed else "unconfirmed"
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


def _declared_session_refs(props: dict[str, Any], run_id: str) -> list[str]:
    """Resolve declarations from their value snapshots, not merged node ownership."""
    facts = facts_of(props)
    claims = claim_provenance_of(props)
    current = facts.get("auth_session_config", {})
    current_sessions = current.get("sessions", []) if isinstance(current, dict) else []
    if not isinstance(current_sessions, list):
        return []
    current_refs = {
        str(item.get("session_ref")) if isinstance(item, dict) else item
        for item in current_sessions
        if ((isinstance(item, dict) and item.get("session_ref")) or isinstance(item, str))
    }
    registrations = facts.get("auth_session_registrations")
    if isinstance(registrations, list):
        claim = claims.get("facts:auth_session_registrations", {})
        elements = claim.get("elements", [])
        if not isinstance(elements, list):
            return []
        return sorted(
            {
                str(item["session_ref"])
                for item in registrations
                if isinstance(item, dict)
                and item.get("session_ref")
                and str(item["session_ref"]) in current_refs
                and (item.get("run_id") is None or str(item["run_id"]) == run_id)
                and any(
                    isinstance(entry, dict)
                    and values_equal(entry.get("value"), item)
                    and (entry.get("run_id") is None or str(entry["run_id"]) == run_id)
                    and entry.get("confidence") == "server_declared"
                    for entry in elements
                )
            }
        )
    # Older seeds used one nested dictionary. Its claim snapshot may be a
    # strict subset of the merged config; only the asserted subset is usable.
    claim = claims.get("facts:auth_session_config", {})
    if claim:
        if claim.get("run_id") is not None and str(claim["run_id"]) != run_id:
            return []
        if claim.get("confidence") != "server_declared":
            return []
        config = claim.get("value", {})
    else:
        owners = {
            str(entry["run_id"])
            for entry in provenance_of(props)
            if isinstance(entry, dict) and entry.get("run_id") is not None
        }
        if owners and owners != {run_id}:
            return []
        config = facts.get("auth_session_config", {})
    raw = config.get("sessions", []) if isinstance(config, dict) else []
    if not isinstance(raw, list):
        return []
    return sorted(
        {
            str(item.get("session_ref")) if isinstance(item, dict) else item
            for item in raw
            if ((isinstance(item, dict) and item.get("session_ref")) or isinstance(item, str))
            and item in current_sessions
        }
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


def _node_observations(
    node: Any, limit: int | None = MAX_OBSERVATIONS_PER_NODE
) -> list[dict[str, Any]]:
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
    if limit is None:
        return out
    return out[:limit]


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


def _fact_view(
    node: Any,
    run_id: str,
    evidence_index: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    observation_limit: int | None = MAX_OBSERVATIONS_PER_NODE,
    evidence_limit: int | None = MAX_EVIDENCE_IDS_PER_ITEM,
) -> tuple:
    """Claim-level fact projection for one node.

    Every attr/fact key needs its own ``measured`` claim-map entry whose
    evidence resolves to a same-run record with a raw payload. A node-level
    measured flag never promotes sibling claims; declared, auth and audit
    keys never become facts. Returns ``(item|None, excluded, historical)``.
    """
    node_type = getattr(node, "type", None)
    if node_type in (GraphNodeType.HYPOTHESIS, GraphNodeType.FINDING):
        return None, [], []
    props = getattr(node, "properties", {}) or {}
    facts = facts_of(props)
    attrs = attrs_of(props)
    first, last = _steps_of(props)
    claims = claim_provenance_of(props)
    index: dict[str, Any] = evidence_index or {}
    diag: dict[str, Any] = diagnostics if diagnostics is not None else {}
    index_failed = bool(diag.get("lookup_failed")) if diagnostics is not None else True
    node_id = str(node.id)
    excluded: list[ExcludedAttr] = []
    historical: list[dict[str, Any]] = []
    kept_attrs: dict[str, Any] = {}
    kept_facts: dict[str, Any] = {}
    grounded_ids: set[str] = set()
    claim_sources: dict[str, dict[str, Any]] = {}

    def _note_evidence(reason: str, eid: str | None) -> None:
        if not eid or diagnostics is None:
            return
        if reason in ("evidence_not_found",):
            diag.setdefault("not_found", []).append(eid)
        elif reason in ("evidence_no_raw",):
            diag.setdefault("no_raw", []).append(eid)
        elif reason in ("historical",):
            diag.setdefault("run_mismatch", []).append(eid)

    def _note_historical(kind: str, key: str, entry: dict[str, Any]) -> None:
        run = entry.get("run_id")
        if run is None:
            run = entry.get("run")
        historical.append(
            {
                "node_id": node_id,
                "kind": kind,
                "key": str(key),
                "run": str(run) if run is not None else None,
                "source": str(entry.get("source", "")),
            }
        )

    for key in sorted(attrs, key=str):
        value = _jsonable(attrs[key])
        if key in DECLARED_ATTR_KEYS:
            excluded.append(
                ExcludedAttr(
                    node_id=node_id, attr=str(key), value=value, reason="declared_never_fact"
                )
            )
            continue
        entry = claims.get("attrs:" + str(key))
        grounded, reason, eid = _claim_grounding(
            entry, run_id=run_id, evidence_index=index, index_failed=index_failed
        )
        if reason == "historical" and isinstance(entry, dict):
            _note_historical("attr", key, entry)
            _note_evidence(reason, eid)
            continue
        if not grounded:
            if key in PLAN_PROMOTED_ATTR_KEYS:
                excluded.append(
                    ExcludedAttr(
                        node_id=node_id,
                        attr=str(key),
                        value=value,
                        reason="plan_promoted_without_observation",
                    )
                )
            else:
                excluded.append(
                    ExcludedAttr(node_id=node_id, attr=str(key), value=value, reason=reason)
                )
            _note_evidence(reason, eid)
            continue
        if not isinstance(entry, dict) or "value" not in entry:
            excluded.append(
                ExcludedAttr(node_id=node_id, attr=str(key), value=value, reason="legacy_no_value")
            )
            _note_evidence(reason, eid)
            continue
        if not values_equal(entry.get("value"), attrs[key]):
            excluded.append(
                ExcludedAttr(node_id=node_id, attr=str(key), value=value, reason="value_changed")
            )
            _note_evidence(reason, eid)
            continue
        kept_attrs[str(key)] = value
        if eid:
            grounded_ids.add(eid)
        if isinstance(entry, dict):
            claim_sources["attrs:" + str(key)] = {
                "source": str(entry.get("source", "")),
                "run": str(
                    entry.get("run_id") if entry.get("run_id") is not None else entry.get("run")
                ),
                "confidence": str(entry.get("confidence", "")),
                "evidence_id": eid,
            }
    for key in sorted(facts, key=str):
        if key in EXCLUDED_FACT_KEYS:
            continue
        if key == "observations":
            continue
        if key in DECLARED_FACT_KEYS:
            excluded.append(
                ExcludedAttr(
                    node_id=node_id,
                    attr=str(key),
                    value=_jsonable(facts[key]),
                    reason="declared_never_fact",
                )
            )
            continue
        if key in AUDIT_FACT_KEYS:
            excluded.append(
                ExcludedAttr(
                    node_id=node_id,
                    attr=str(key),
                    value=_jsonable(facts[key]),
                    reason="audit_not_fact",
                )
            )
            continue
        entry = claims.get("facts:" + str(key))
        grounded, reason, eid = _claim_grounding(
            entry, run_id=run_id, evidence_index=index, index_failed=index_failed
        )
        if reason == "historical" and isinstance(entry, dict):
            _note_historical("fact", key, entry)
            _note_evidence(reason, eid)
            continue
        if not grounded:
            excluded.append(
                ExcludedAttr(
                    node_id=node_id, attr=str(key), value=_jsonable(facts[key]), reason=reason
                )
            )
            _note_evidence(reason, eid)
            continue
        merged_value = facts[key]
        if not isinstance(entry, dict) or "value" not in entry:
            excluded.append(
                ExcludedAttr(
                    node_id=node_id,
                    attr=str(key),
                    value=_jsonable(merged_value),
                    reason="legacy_no_value",
                )
            )
            _note_evidence(reason, eid)
            continue
        if isinstance(merged_value, list):
            # Element-level verdict: only elements a grounded measured write
            # asserted survive. Older declared/inferred elements merged into
            # the same list are dropped and reported as diagnostics.
            element_map: dict[str, dict[str, Any]] = {}
            raw_elements = entry.get("elements", [])
            if isinstance(raw_elements, list):
                for element in raw_elements:
                    if isinstance(element, dict) and element.get("digest"):
                        element_map.setdefault(str(element["digest"]), element)
            kept_elements: list[Any] = []
            dropped = 0
            dropped_sample: list[Any] = []
            for item in merged_value:
                element = element_map.get(digest_value(item))
                if element is None:
                    dropped += 1
                    if len(dropped_sample) < 5:
                        dropped_sample.append(_jsonable(item))
                    continue
                element_grounded, element_reason, element_eid = _claim_grounding(
                    element, run_id=run_id, evidence_index=index, index_failed=index_failed
                )
                if element_reason == "historical":
                    _note_historical("fact_element", key, element)
                    _note_evidence(element_reason, element_eid)
                    dropped += 1
                    if len(dropped_sample) < 5:
                        dropped_sample.append(_jsonable(item))
                    continue
                if not element_grounded:
                    dropped += 1
                    if len(dropped_sample) < 5:
                        dropped_sample.append(_jsonable(item))
                    _note_evidence(element_reason, element_eid)
                    continue
                kept_elements.append(_jsonable(item))
                if element_eid:
                    grounded_ids.add(element_eid)
            if dropped and diagnostics is not None:
                diag.setdefault("excluded_elements", []).append(
                    {
                        "node_id": node_id,
                        "key": str(key),
                        "excluded_count": dropped,
                        "sample": dropped_sample,
                    }
                )
            if not kept_elements:
                excluded.append(
                    ExcludedAttr(
                        node_id=node_id,
                        attr=str(key),
                        value=_jsonable(merged_value),
                        reason="no_grounded_elements",
                    )
                )
                continue
            kept_facts[str(key)] = kept_elements
            if isinstance(entry, dict):
                claim_sources["facts:" + str(key)] = {
                    "source": str(entry.get("source", "")),
                    "run": str(
                        entry.get("run_id") if entry.get("run_id") is not None else entry.get("run")
                    ),
                    "confidence": str(entry.get("confidence", "")),
                    "evidence_id": eid,
                    "grounded_elements": len(kept_elements),
                    "total_elements": len(merged_value),
                }
            continue
        if not values_equal(entry.get("value"), merged_value):
            excluded.append(
                ExcludedAttr(
                    node_id=node_id,
                    attr=str(key),
                    value=_jsonable(merged_value),
                    reason="value_changed",
                )
            )
            _note_evidence(reason, eid)
            continue
        kept_facts[str(key)] = _jsonable(merged_value)
        if eid:
            grounded_ids.add(eid)
        if isinstance(entry, dict):
            claim_sources["facts:" + str(key)] = {
                "source": str(entry.get("source", "")),
                "run": str(
                    entry.get("run_id") if entry.get("run_id") is not None else entry.get("run")
                ),
                "confidence": str(entry.get("confidence", "")),
                "evidence_id": eid,
            }
    kept_observations: list[dict[str, Any]] = []
    for obs in _node_observations(node, observation_limit):
        obs_eid = obs.get("evidence_id") if isinstance(obs, dict) else None
        context_key = str(obs.get("context_key", "")) if isinstance(obs, dict) else ""
        if not obs_eid or str(obs_eid) not in index or not _record_has_raw(index.get(str(obs_eid))):
            reason = "evidence_lookup_failed" if index_failed else "observation_ungrounded"
            excluded.append(
                ExcludedAttr(
                    node_id=node_id,
                    attr="observations:" + context_key,
                    value=_jsonable(obs),
                    reason=reason,
                )
            )
            if obs_eid and not index_failed:
                _note_evidence("evidence_not_found", str(obs_eid))
            continue
        kept_observations.append(obs)
        grounded_ids.add(str(obs_eid))
    all_ids = _node_evidence_ids(node)
    if evidence_limit is not None and len(all_ids) > evidence_limit and diagnostics is not None:
        diag.setdefault("truncated", []).append(
            {
                "node_id": node_id,
                "omitted_evidence_ids": all_ids[evidence_limit:],
            }
        )
    evidence_ids = all_ids[:evidence_limit] if evidence_limit is not None else list(all_ids)
    if not kept_attrs and not kept_facts and not kept_observations:
        return None, excluded, historical
    item = FactItem(
        node_id=node_id,
        node_type=str(
            node_type.value if node_type is not None and hasattr(node_type, "value") else node_type
        ),
        observed_attrs=kept_attrs,
        observed_facts=kept_facts,
        observations=kept_observations,
        evidence_ids=evidence_ids,
        provenance=_provenance_summary(props),
        first_seen_step=first,
        last_seen_step=last,
        origin="observed",
        grounded_evidence_ids=_sorted_str(grounded_ids),
        claim_sources=claim_sources,
    )
    return item, excluded, historical


def _priority(last_seen: int, node_id: str, targets: set[str], step: int) -> tuple:
    in_target = 0 if node_id in targets else 1
    future = 0 if last_seen <= step else 1
    return (in_target, future, -last_seen, node_id)


async def _hypothesis_items(
    graph_service: Any,
    evidence_index: dict[str, Any] | None,
    run_id: str,
    targets: set[str],
    step: int,
) -> list:
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
    by_hyp: dict[str, list[str]] = {}
    for eid, rec in (evidence_index or {}).items():
        hid = str(getattr(rec, "hypothesis_id", "") or "")
        if hid:
            by_hyp.setdefault(hid, []).append(str(eid))
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
            first, last = _steps_of(getattr(node, "properties", {}) or {})
        node_evidence: list[str] = []
        if node is not None:
            node_evidence.extend(_node_evidence_ids(node))
        meta_evidence: list[str] = []
        for key in ("evidence_ids", "supporting_evidence_ids", "refuting_evidence_ids"):
            raw = meta.get(key, [])
            if isinstance(raw, list):
                meta_evidence.extend([str(v) for v in raw])
        linked = _sorted_str(node_evidence + meta_evidence + by_hyp.get(hid, []))[
            :MAX_EVIDENCE_IDS_PER_ITEM
        ]
        status = str(hyp.status or "open")
        # Support/refute needs an explicit record: an inconclusive outcome is
        # not refuting evidence, and an open TARGETS link is not supporting
        # evidence. Everything else stays neutral in evidence_ids.
        explicit_supporting: list[str] = []
        explicit_refuting: list[str] = []
        raw_support = meta.get("supporting_evidence_ids", [])
        raw_refuting = meta.get("refuting_evidence_ids", [])
        explicit_supporting = (
            _sorted_str([str(v) for v in raw_support]) if isinstance(raw_support, list) else []
        )
        explicit_refuting = (
            _sorted_str([str(v) for v in raw_refuting]) if isinstance(raw_refuting, list) else []
        )
        supporting = _sorted_str(explicit_supporting)[:MAX_EVIDENCE_IDS_PER_ITEM]
        refuting = _sorted_str(explicit_refuting)[:MAX_EVIDENCE_IDS_PER_ITEM]
        neutral = [eid for eid in linked if eid not in set(supporting) and eid not in set(refuting)]
        meta_target = meta.get("target_node_id", None)
        edge_targets = sorted(set(target_links.get(hid, [])))
        target_node_id = (
            str(meta_target)
            if isinstance(meta_target, str) and meta_target
            else (edge_targets[0] if edge_targets else None)
        )
        related = _sorted_str(edge_targets + finding_links.get(hid, []))
        rels: list[dict[str, Any]] = []
        hnode = str(hypothesis_node_id(hid))
        for edge in edge_list:
            if str(edge.source_id) == hnode or str(edge.target_id) == hnode:
                rels.append(
                    {
                        "edge_type": str(
                            edge.type.value if hasattr(edge.type, "value") else edge.type
                        ),
                        "source_id": str(edge.source_id),
                        "target_id": str(edge.target_id),
                    }
                )
        rels.sort(key=lambda d: (d["edge_type"], d["source_id"], d["target_id"]))
        needs: list[str] = []
        if status == "open":
            if target_node_id:
                needs.append(
                    "observe "
                    + target_node_id
                    + " with a tool that records evidence for this claim"
                )
            else:
                needs.append("pin the claim to one concrete target node before further validation")
            exp = meta.get("expectation", None)
            if isinstance(exp, dict) and exp.get("description"):
                needs.append("check expectation: " + str(exp.get("description"))[:240])
            else:
                needs.append(
                    "record supporting or refuting evidence with the hypothesis id attached"
                )
        expectation = (
            _jsonable(meta.get("expectation")) if meta.get("expectation") is not None else None
        )
        if expectation is not None and not isinstance(expectation, dict):
            expectation = {"value": expectation}
        items.append(
            HypothesisItem(
                hypothesis_id=hid,
                claim=str(hyp.description or ""),
                confidence=float(hyp.confidence or 0.0),
                status=status,
                target_node_id=target_node_id,
                target=str(meta.get("target")) if meta.get("target") is not None else None,
                agent_id=str(meta.get("agent_id")) if meta.get("agent_id") is not None else None,
                step_id=str(meta.get("step_id")) if meta.get("step_id") is not None else None,
                violation_if_confirmed=bool(meta.get("violation_if_confirmed"))
                if meta.get("violation_if_confirmed") is not None
                else None,
                severity=str(meta.get("severity")) if meta.get("severity") is not None else None,
                expectation=expectation,
                supporting_evidence_ids=supporting,
                refuting_evidence_ids=refuting,
                evidence_ids=linked,
                neutral_evidence_ids=neutral,
                finding_ids=_sorted_str(
                    [
                        s.partition(":")[2] if s.startswith("finding:") else s
                        for s in finding_links.get(hid, [])
                    ]
                ),
                related_node_ids=related,
                relations=rels,
                needs=needs,
                first_seen_step=first,
                last_seen_step=last,
                origin="inferred",
            )
        )

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
            hyp_links.setdefault(fid, []).append(
                tgt.partition(":")[2] if tgt.startswith("hypothesis:") else tgt
            )
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
        items.append(
            FindingItem(
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
            )
        )

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


async def _known_targets(
    nodes: list,
    run_id: str,
    targets: set[str],
    step: int,
    evidence_index: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    grounded_node_ids: set[str] | None = None,
    provided_node_ids: set[str] | None = None,
) -> tuple:
    """Provided targets plus actually observed ones (grounded only).

    Non-provided nodes join only with grounded measured content from this
    run; other-run claims are reported as historical, never merged in.
    Returns ``(known, historical_targets)``.
    """
    out: list[KnownTarget] = []
    historical_targets: list[dict[str, Any]] = []
    index: dict[str, Any] = evidence_index or {}
    index_failed = (
        bool((diagnostics or {}).get("lookup_failed")) if diagnostics is not None else True
    )
    grounded: set[str] = grounded_node_ids or set()
    for node in nodes:
        ntype = getattr(node, "type", None)
        if ntype not in USABLE_TARGET_TYPES:
            continue
        props = getattr(node, "properties", {}) or {}
        provided = (
            _is_provided_node(node)
            if provided_node_ids is None
            else str(node.id) in provided_node_ids
        )
        if provided:
            origin = "declared"
            confirmed = _node_has_grounded_presence(
                props, run_id=run_id, evidence_index=index, index_failed=index_failed
            )
            verification = "confirmed" if confirmed else "unconfirmed"
        else:
            if str(node.id) in grounded:
                origin = "observed"
                verification = "confirmed"
            elif ntype in (
                GraphNodeType.PRINCIPAL,
                GraphNodeType.CREDENTIAL,
            ) and _node_has_grounded_presence(
                props, run_id=run_id, evidence_index=index, index_failed=index_failed
            ):
                origin = "observed"
                verification = "confirmed"
            else:
                runs: list[str] = []
                for claim_entry in claim_provenance_of(props).values():
                    if not isinstance(claim_entry, dict):
                        continue
                    claim_run = claim_entry.get("run_id")
                    if claim_run is None:
                        claim_run = claim_entry.get("run")
                    if (
                        claim_run is not None
                        and str(claim_run) != ""
                        and str(claim_run) != str(run_id)
                    ):
                        runs.append(str(claim_run))
                runs = _sorted_str(runs)
                if runs:
                    historical_targets.append({"node_id": str(node.id), "runs": runs})
                continue
        address, target_ref, observed = _target_address(node)
        if not address:
            continue
        _, last_seen = _steps_of(props)
        out.append(
            KnownTarget(
                node_id=str(node.id),
                node_type=str(ntype.value if hasattr(ntype, "value") else ntype),
                address=str(address),
                target_ref=target_ref,
                observed_target=observed,
                origin=origin,
                verification=verification,
                last_seen_step=last_seen,
            )
        )
    out.sort(key=lambda t: _priority(t.last_seen_step, t.node_id, targets, step))
    return out, historical_targets


def _fact_refs_for_item(item: FactItem) -> list[str]:
    refs: list[str] = [str(item.node_id)]
    for key in item.observed_attrs or {}:
        refs.append(str(item.node_id) + "#attr:" + str(key))
    for key in item.observed_facts or {}:
        refs.append(str(item.node_id) + "#fact:" + str(key))
    for obs in item.observations or []:
        if isinstance(obs, dict) and obs.get("context_key"):
            refs.append(str(item.node_id) + "#obs:" + str(obs.get("context_key")))
    # fact refs admit only grounded same-run evidence; the raw node
    # evidence-id list stays on the item as metadata, never as refs.
    for eid in _sorted_str(item.grounded_evidence_ids or [])[:MAX_EVIDENCE_IDS_PER_ITEM]:
        refs.append("evidence:" + str(eid))
    out: list[str] = []
    for r in refs:
        if r not in out:
            out.append(r)
    return sorted(out)


async def project_knowledge(
    graph_service: Any,
    evidence_store: Any,
    run_id: str,
    step: int = 0,
    target_node_ids: list[str] | None = None,
    _inputs: tuple | None = None,
) -> dict:
    run = str(run_id)
    targets: set[str] = set([str(v) for v in (target_node_ids or [])])
    if _inputs is None:
        node_list, graph_error, evidence_index, evidence_diagnostics = await _collect_inputs(
            graph_service, evidence_store, run
        )
    else:
        node_list, graph_error, evidence_index, evidence_diagnostics = _inputs
    index_failed = bool(evidence_diagnostics.get("lookup_failed"))
    provided_all: list[ProvidedClaim] = []
    for node in sorted(node_list, key=lambda n: str(n.id)):
        if _is_provided_node(node):
            provided_all.append(_provided_claim(node, run, evidence_index, index_failed))
    provided_all.sort(key=lambda c: _priority(c.last_seen_step, c.node_id, targets, step))
    provided_ids = [c.node_id for c in provided_all]
    provided_kept, provided_omitted = _truncate_ids(provided_ids, MAX_PROVIDED_ITEMS)
    provided_map = {c.node_id: c for c in provided_all}
    provided = [provided_map[i].model_dump(mode="json") for i in provided_kept]

    fact_all: list[FactItem] = []
    excluded_all: list[ExcludedAttr] = []
    historical_facts: list[dict[str, Any]] = []
    for node in sorted(node_list, key=lambda n: str(n.id)):
        item, exc, hist = _fact_view(node, run, evidence_index, evidence_diagnostics)
        excluded_all.extend(exc)
        historical_facts.extend(hist)
        if item is not None:
            fact_all.append(item)
    grounded_node_ids = set(item.node_id for item in fact_all)
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

    hyps = await _hypothesis_items(graph_service, evidence_index, run, targets, step)
    hyp_ids = [h.hypothesis_id for h in hyps]
    hyp_kept, hyp_omitted = _truncate_ids(hyp_ids, MAX_HYPOTHESIS_ITEMS)
    hyp_map = {h.hypothesis_id: h for h in hyps}
    hypotheses = [hyp_map[i].model_dump(mode="json") for i in hyp_kept]

    finds = await _finding_items(graph_service, run, targets, step)
    find_ids = [f.finding_id for f in finds]
    find_kept, find_omitted = _truncate_ids(find_ids, MAX_FINDING_ITEMS)
    find_map = {f.finding_id: f for f in finds}
    findings = [find_map[i].model_dump(mode="json") for i in find_kept]

    known_all, historical_targets = await _known_targets(
        node_list, run, targets, step, evidence_index, evidence_diagnostics, grounded_node_ids
    )
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
        "historical": {
            "facts": historical_facts,
            "targets": historical_targets,
            "note": "other-run claims are listed here, never merged into facts/known_targets",
        },
        "evidence_diagnostics": _finalize_diagnostics(evidence_diagnostics),
        "graph_diagnostics": {
            "lookup_failed": graph_error is not None,
            "lookup_error": graph_error,
        },
    }


def _finalize_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Dedupe and cap diagnostic lists (lookup failure stays explicit)."""
    final: dict[str, Any] = {
        "lookup_failed": bool(diagnostics.get("lookup_failed", False)),
        "lookup_error": diagnostics.get("lookup_error"),
    }
    for key in ("not_found", "no_raw", "run_mismatch"):
        raw = diagnostics.get(key, [])
        ids = [str(v) if not isinstance(v, dict) else str(v) for v in (raw or [])]
        final[key] = _sorted_str(ids)[:MAX_DIAGNOSTIC_IDS]
    truncated = diagnostics.get("truncated", [])
    final["truncated"] = list(truncated or [])[:MAX_DIAGNOSTIC_IDS]
    excluded_elements = diagnostics.get("excluded_elements", [])
    final["excluded_elements"] = list(excluded_elements or [])[:MAX_DIAGNOSTIC_IDS]
    final["excluded_element_count"] = len(excluded_elements or [])
    return final


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
    "DECLARED_ATTR_KEYS",
    "DECLARED_FACT_KEYS",
    "AUDIT_FACT_KEYS",
    "project_knowledge",
    "collect_assessment",
    "collect_knowledge",
]


async def _collect_inputs(
    graph_service: Any, evidence_store: Any, run: str, *, engagement_id: str | None = None
) -> tuple[list, str | None, dict[str, Any], dict[str, Any]]:
    """One shared store read: nodes plus same-run evidence.
    A node-listing failure is returned as an error name, never folded
    into an empty list, so checks can tell failure apart from emptiness."""
    try:
        nodes = await graph_service.list_nodes()
    except Exception as exc:
        graph_error: str | None = type(exc).__name__
        nodes = []
    else:
        graph_error = None
    node_list = list(nodes or [])
    evidence_index, evidence_diagnostics = await _build_evidence_index(
        evidence_store, run, engagement_id=engagement_id
    )
    return node_list, graph_error, evidence_index, evidence_diagnostics


def _snapshot_bindings(
    fact_items: list[FactItem], provided_claims: list[ProvidedClaim]
) -> dict[str, set[str]]:
    """Principal and session wiring from full items, refs only, never secrets.
    Same rules as the display-side binding read, over untruncated items."""
    bindings: dict[str, set[str]] = {"anonymous": set()}
    items: list[FactItem | ProvidedClaim] = [*fact_items, *provided_claims]
    for obj in items:
        if isinstance(obj, FactItem):
            facts = obj.observed_facts or {}
            node_type = obj.node_type
            node_id = obj.node_id
        else:
            facts = {}
            node_type = obj.node_type
            node_id = obj.node_id
            declared = [str(reference) for reference in obj.auth_session_refs or []]
            for reference in declared:
                bindings.setdefault(reference, set()).add(reference)
            if node_type in {"principal", "credential"}:
                bindings.setdefault(str(node_id), set()).update(declared)
        if not isinstance(facts, dict):
            continue
        config = facts.get("auth_session_config", {})
        sessions = config.get("sessions", []) if isinstance(config, dict) else []
        for session in sessions or []:
            if isinstance(session, dict) and session.get("session_ref"):
                reference = str(session["session_ref"])
                bindings.setdefault(reference, set()).add(reference)
        refs = facts.get("session_refs", [])
        if facts.get("session_ref"):
            refs = [*refs, facts["session_ref"]]
        if facts.get("principal_ref"):
            bindings.setdefault(str(facts["principal_ref"]), set()).update(map(str, refs))
        if node_type in {"principal", "credential"}:
            bindings.setdefault(str(node_id), set()).update(map(str, refs))
    return bindings


async def _build_snapshot_from_inputs(
    node_list: list,
    evidence_index: dict[str, Any],
    evidence_diagnostics: dict[str, Any],
    graph_error: str | None,
    run: str,
    step: int,
    selected: set[str],
) -> InternalAssessment:
    """Full pre-truncation material from one shared read."""
    if graph_error is not None:
        return InternalAssessment(
            run_id=run,
            graph_lookup_failed=True,
            graph_error=graph_error,
            evidence_lookup_failed=bool(evidence_diagnostics.get("lookup_failed")),
            evidence_error=evidence_diagnostics.get("lookup_error"),
            principal_bindings={"anonymous": set()},
        )
    index_failed = bool(evidence_diagnostics.get("lookup_failed"))
    snap_diag: dict[str, Any] = {
        "lookup_failed": index_failed,
        "lookup_error": evidence_diagnostics.get("lookup_error"),
        "not_found": [],
        "no_raw": [],
        "run_mismatch": [],
        "truncated": [],
        "excluded_elements": [],
    }
    provided_all: list[ProvidedClaim] = []
    provided_ids: set[str] = set()
    for node in sorted(node_list, key=lambda n: str(n.id)):
        if _is_provided_node(node):
            # Legacy declarations without run ids remain readable; explicitly
            # foreign registrations cannot authorize this run's targets/sessions.
            registrations = [
                entry
                for entry in provenance_of(getattr(node, "properties", {}) or {})
                if isinstance(entry, dict)
                and (
                    entry.get("confidence") == "server_declared"
                    or str(entry.get("source", "")).startswith("engagement:")
                )
                and entry.get("run_id") is not None
            ]
            if registrations and not any(str(entry["run_id"]) == run for entry in registrations):
                continue
            provided_ids.add(str(node.id))
            provided_all.append(_provided_claim(node, run, evidence_index, index_failed))
    fact_all: list[FactItem] = []
    historical_fact_count = 0
    for node in sorted(node_list, key=lambda n: str(n.id)):
        item, _, hist = _fact_view(
            node,
            run,
            evidence_index,
            snap_diag,
            observation_limit=None,
            evidence_limit=None,
        )
        historical_fact_count += len(hist)
        if item is not None:
            fact_all.append(item)
    grounded_nodes = {item.node_id for item in fact_all}
    known_all, historical_targets = await _known_targets(
        node_list, run, selected, step, evidence_index, snap_diag, grounded_nodes, provided_ids
    )
    targets = {
        item.node_id: InternalTarget(
            node_id=item.node_id,
            node_type=item.node_type,
            origin=item.origin,
            verification=item.verification,
            address=item.address,
            target_ref=item.target_ref,
        )
        for item in known_all
    }
    refs: set[str] = set()
    for item in fact_all:
        refs.add(str(item.node_id))
        for key in item.observed_attrs or {}:
            refs.add(str(item.node_id) + "#attr:" + str(key))
        for key in item.observed_facts or {}:
            refs.add(str(item.node_id) + "#fact:" + str(key))
        for obs in item.observations or []:
            if isinstance(obs, dict) and obs.get("context_key"):
                refs.add(str(item.node_id) + "#obs:" + str(obs.get("context_key")))
        for eid in item.grounded_evidence_ids or []:
            refs.add("evidence:" + str(eid))
    return InternalAssessment(
        run_id=run,
        targets=targets,
        fact_refs=refs,
        principal_bindings=_snapshot_bindings(fact_all, provided_all),
        evidence_lookup_failed=index_failed,
        evidence_error=evidence_diagnostics.get("lookup_error"),
        historical_targets=[dict(h) for h in (historical_targets or [])],
        historical_fact_count=historical_fact_count,
        # Execution deduplication follows full measured content, never prompt
        # selection, receipt names or evidence bookkeeping.
        fact_fingerprint=knowledge_fingerprint(
            {
                "facts": [
                    item.model_dump(
                        include={
                            "node_id",
                            "node_type",
                            "observed_attrs",
                            "observed_facts",
                            "observations",
                        }
                    )
                    for item in fact_all
                ]
            }
        ),
    )


async def collect_assessment(
    graph_service: Any,
    evidence_store: Any,
    run_id: str,
    step: int = 0,
    target_node_ids: list[str] | None = None,
    *,
    engagement_id: str | None = None,
) -> InternalAssessment:
    """Build the full pre-truncation material for one check.
    Same claim-level grounding as the display projection, but nothing is
    cut. Callers collect fresh per check and never cache the result."""
    run = str(run_id)
    selected: set[str] = {str(v) for v in (target_node_ids or [])}
    node_list, graph_error, evidence_index, evidence_diagnostics = await _collect_inputs(
        graph_service, evidence_store, run, engagement_id=engagement_id
    )
    return await _build_snapshot_from_inputs(
        node_list, evidence_index, evidence_diagnostics, graph_error, run, step, selected
    )


async def collect_progress_content(
    graph_service: Any,
    evidence_store: Any,
    run_id: str,
    *,
    step: int,
    engagement_id: str | None = None,
) -> str | None:
    """Full grounded facts, supported conclusions and usable declarations."""
    nodes, graph_error, index, diagnostics = await _collect_inputs(
        graph_service, evidence_store, run_id, engagement_id=engagement_id
    )
    if graph_error is not None or diagnostics.get("lookup_failed"):
        return None
    material = await _build_snapshot_from_inputs(
        nodes, index, diagnostics, graph_error, run_id, step, set()
    )
    try:
        hypotheses = await HypothesisService(graph_service).list()
    except Exception:  # noqa: BLE001 - not a changed or empty investigation
        return None
    conclusions = []
    for hyp in hypotheses:
        if hyp.status not in {"confirmed", "refuted", "rejected"}:
            continue
        meta = hyp.metadata or {}
        refs = {
            str(eid)
            for key in ("evidence_ids", "supporting_evidence_ids", "refuting_evidence_ids")
            for eid in (meta.get(key, []) if isinstance(meta.get(key, []), list) else [])
        }
        supported = any(
            _record_has_raw(record)
            and (eid in refs or str(record.hypothesis_id or "") == str(hyp.hypothesis_id))
            for eid, record in index.items()
        )
        if supported:
            conclusions.append(
                {
                    "claim": hyp.description,
                    "status": hyp.status,
                    "target_node_id": meta.get("target_node_id"),
                    "expectation": meta.get("expectation"),
                    # Used only as a grounding gate by knowledge_fingerprint.
                    "evidence_ids": ["grounded"],
                }
            )
    return knowledge_fingerprint(
        {
            "facts": [
                {
                    "measured": material.fact_fingerprint,
                    "targets": [vars(item) for item in material.targets.values()],
                    "principal_bindings": {
                        key: sorted(value) for key, value in material.principal_bindings.items()
                    },
                }
            ],
            "hypotheses": conclusions,
        }
    )


async def collect_knowledge(
    graph_service: Any,
    evidence_store: Any,
    run_id: str,
    step: int = 0,
    target_node_ids: list[str] | None = None,
    *,
    engagement_id: str | None = None,
) -> tuple[dict, InternalAssessment]:
    """One shared read feeding both the display dict and the material.
    The capped projection keeps its exact shape for model consumers;
    the assessment carries the full refs for execution checks."""
    run = str(run_id)
    selected: set[str] = {str(v) for v in (target_node_ids or [])}
    node_list, graph_error, evidence_index, evidence_diagnostics = await _collect_inputs(
        graph_service, evidence_store, run, engagement_id=engagement_id
    )
    snapshot = await _build_snapshot_from_inputs(
        node_list, evidence_index, evidence_diagnostics, graph_error, run, step, selected
    )
    display = await project_knowledge(
        graph_service,
        evidence_store,
        run_id,
        step=step,
        target_node_ids=target_node_ids,
        _inputs=(node_list, graph_error, evidence_index, evidence_diagnostics),
    )
    return display, snapshot
