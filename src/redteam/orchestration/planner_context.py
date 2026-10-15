"""One ephemeral, read-only Planner pass; canonical admission remains separate."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.core.contracts.mission import redact_secret_material
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    error_type_for,
    exception_chain_for,
)
from redteam.orchestration.evidence_access import (
    EvidenceReader,
    project_original_records,
    project_runtime_records,
)
from redteam.orchestration.investigation_context import (
    ContextQuery,
    ContextReadError,
    ContextRef,
    InvestigationContextReader,
    InvestigationSnapshot,
    context_bytes,
)


class PlannerEvidenceQuery(BaseModel):
    """Search captured public records, then read a window through EvidenceReader."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    mode: Literal["list", "read"] = "list"
    evidence_id: str | None = Field(default=None, min_length=1, max_length=512)
    task_id: str | None = Field(default=None, min_length=1, max_length=512)
    hypothesis_id: str | None = Field(default=None, min_length=1, max_length=512)
    record_kind: str | None = Field(default=None, min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    count: int = Field(default=8, ge=1, le=20)
    limit: int = Field(default=4000, ge=1, le=12000)
    query: str | None = Field(default=None, min_length=1, max_length=256)
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("evidence_id", "task_id", "hypothesis_id")
    @classmethod
    def local_reference(cls, value):
        if value is not None:
            ContextRef(kind="task", identity=value)
        return value

    @model_validator(mode="after")
    def read_identity(self):
        # Typed codes are Pydantic-owned rule names with fixed templates; never input text.
        if self.mode == "read" and self.evidence_id is None:
            raise PydanticCustomError(
                "planner_evidence_read_needs_evidence_id", "Read mode needs evidence_id.")
        if self.mode == "list" and self.evidence_id is not None:
            raise PydanticCustomError(
                "planner_evidence_list_rejects_evidence_id",
                "List mode takes filters, not evidence_id.")
        return self


class PlannerContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1, 2] = 1
    context: ContextQuery | None = None
    evidence: PlannerEvidenceQuery | None = None
    queries: list[PlannerContextBatchItem] | None = Field(default=None, min_length=1,
                                                              max_length=4)

    @model_validator(mode="after")
    def one_query(self):
        if self.version == 1 and ((self.context is None) == (self.evidence is None)
                                  or self.queries is not None):
            raise PydanticCustomError(
                "planner_lookup_needs_exactly_one_query",
                "Send exactly one context or evidence query in context_request.")
        if self.version == 2 and (self.context is not None or self.evidence is not None
                                  or self.queries is None):
            raise PydanticCustomError(
                "planner_lookup_needs_exactly_one_query",
                "Version 2 uses a queries array, not top-level context or evidence.")
        return self


class PlannerContextBatchItem(BaseModel):
    """One independently bounded read in a version-2 lookup batch."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    context: ContextQuery | None = None
    evidence: PlannerEvidenceQuery | None = None

    @model_validator(mode="after")
    def one_query(self):
        if (self.context is None) == (self.evidence is None):
            raise PydanticCustomError(
                "planner_lookup_needs_exactly_one_query",
                "Each batch item needs exactly one context or evidence query.")
        return self


PlannerLookupRequest = PlannerContextRequest


class PlannerReviewedReferenceError(ContextReadError):
    """Code-owned diagnostic; the shared validator keeps its exact code and location."""

    def __init__(self, field: str):
        code = "planner_reviewed_reference_not_presented"
        location = f"$.{field}"
        self.diagnostic = DecisionDiagnostic(
            code=code, stage="hypothesis_planning", kind="validation", location=location,
            summary="A reviewed task or candidate must be presented in this pass. Read or "
                    "list its original with context_request first, then cite only refs "
                    "that appear in that result, or remove the unseen ID.",
            retryable=True, cause_scope="model_input", owner="planner_context",
            repair_condition="The cited task or candidate appears in the captured context "
                             "or a lookup result of this pass.",
            validation_issues=[{"rule": code, "location": location}],
        )
        super().__init__(code)


# Owned rule code -> (fixed contract location, static correction). Pydantic supplies only the
# code; messages, inputs and contexts are never read or copied.
LOOKUP_RULE_GUIDANCE: dict[str, tuple[str, str]] = {
    "planner_lookup_needs_exactly_one_query": (
        "$.context_request",
        "Use version 1 with exactly one context or evidence query, or version 2 with only a "
        "queries array containing one to four items. Each item has exactly one query.",
    ),
    "planner_evidence_read_needs_evidence_id": (
        "$.context_request.evidence",
        "Read mode needs one evidence_id copied from a list result.",
    ),
    "planner_evidence_list_rejects_evidence_id": (
        "$.context_request.evidence",
        "List mode uses filters and count; remove evidence_id or switch to read mode.",
    ),
}


def planner_lookup_diagnostic(exc: BaseException, *, stage: str = "hypothesis_planning"
                              ) -> DecisionDiagnostic | None:
    """Owned diagnostic for a Pydantic lookup rule, or None for every other failure."""
    if not isinstance(exc, ValidationError):
        return None
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    matched = [error for error in errors if error.get("type") in LOOKUP_RULE_GUIDANCE]
    if not matched:
        return None
    code = str(matched[0]["type"])
    location, summary = LOOKUP_RULE_GUIDANCE[code]
    return DecisionDiagnostic(
        code=code, stage=stage, kind="format", location=location, summary=summary,
        retryable=True, cause_scope="model_input", owner="planner_context",
        repair_condition="The context_request names exactly the documented lookup query.",
        validation_issues=[
            {"rule": str(error["type"]), "location": LOOKUP_RULE_GUIDANCE[str(error["type"])][0]}
            for error in matched[:10]
        ],
        error_type=error_type_for(exc), exception_chain=exception_chain_for(exc),
    )


def lookup_only(decision) -> bool:
    if decision.context_request is None:
        return False
    if (decision.updates or getattr(decision, "tasks", ())
            or getattr(decision, "questions", ()) or decision.reviewed_task_ids
            or decision.reviewed_hypothesis_ids or decision.termination != "continue"
            or decision.resume_conditions):
        raise ContextReadError("planner_lookup_cannot_commit")
    return True


def project_planner_knowledge(knowledge):
    """Keep existing grounded views, without unbounded omitted-ID indexes."""
    result = {"note": "Bounded knowledge DATA; omitted nodes remain available through "
                      "context_request node list/read, not evidence of absence.", "counts": {}}
    groups = {"facts": knowledge.get("facts", []),
              "known_targets": knowledge.get("known_targets", []),
              "provided_claims": knowledge.get("provided", {}).get("claims", []),
              "findings": knowledge.get("findings", {}).get("items", [])}
    remaining = 16384
    for key, items in groups.items():
        selected = []
        for item in items[:8]:
            size = context_bytes(item)
            if size <= remaining:
                selected.append(item)
                remaining -= size
        result[key] = selected
        result["counts"][key] = {"shown": len(selected), "captured_view_count": len(items),
                                  "omitted_count": len(items) - len(selected)}
    result["graph_diagnostics"] = knowledge.get("graph_diagnostics", {})
    return result


def public_receipt_metadata(metadata):
    return {key: metadata[key] for key in (
        "record_kind", "public", "admission", "posthoc", "origin", "supersedes",
        "reviewer_id", "hypothesis_id", "candidate_revision", "candidate_purpose_hash",
        "verdict", "hypothesis_outcome", "outcome", "claim_supported", "claim", "claim_identity",
        "reason", "action_id", "invocation_id", "target_node_id", "claimed_principals",
        "covered_principals", "source_evidence_ids", "source_origins",
        "supporting_evidence_ids", "refuting_evidence_ids",
    ) if key in metadata}


class _PublicReceiptRead:
    """Read adapter: EvidenceReader retains windowing, provenance and scope checks."""

    def __init__(self, store):
        self.store = store

    async def get(self, identity):
        record = await self.store.get(identity)
        if (record is not None and record.metadata.get("record_kind") == "review_receipt"
                and record.metadata.get("public") is True):
            return record.model_copy(update={"metadata": public_receipt_metadata(record.metadata)})
        return record


class PlannerPassContext:
    """Bounded prompt views and consumed read identities, never a second state store.

    Source data is captured once. Bounded recent lookup windows are retained for the next
    judgment; only identity/hash read sets accumulate internally for the existing CAS.
    """

    def __init__(self, snapshot, records, context, *, run_id, engagement_id, evidence_store,
                 graph):
        self.canonical = snapshot
        self.store = evidence_store
        self.graph = graph
        self.records = {str(record.evidence_id): record for record in records
                        if str(record.run_id) == str(run_id)
                        and str(record.engagement_id) == str(engagement_id)}
        self.validity = {item["evidence_id"]: item for item in context["evidence_validity"]}
        self.reader = InvestigationContextReader(InvestigationSnapshot.create(
            run_id=str(run_id), engagement_id=str(engagement_id), mission=snapshot.mission,
            binding_hash=snapshot.binding, tasks=list(snapshot.tasks.values()),
            candidates=list(snapshot.candidates.values()), executions=list(snapshot.executions),
            nodes=list(snapshot.nodes.values()), edges=list(snapshot.edges),
            graph_roots=snapshot.graph_roots, graph_truncated=snapshot.graph_truncated,
        ))
        self.seen: dict[str, set[str]] = {kind: set() for kind in (
            "task", "candidate", "execution", "node", "edge")}
        self.consumed = {kind: set() for kind in self.seen}
        self.used_evidence: set[str] = set()
        self.cache: dict[str, dict] = {}
        self.last_request: dict | None = None
        self.recent_windows: list[dict] = []
        self._recent_seen: list[set[tuple[str, str]]] = []
        self._lookup_visible: set[tuple[str, str]] = set()
        self.pending_batch_request: dict | None = None
        recent = sorted(self.records.values(), key=lambda record: record.created_at, reverse=True)
        feedback = tuple(dict.fromkeys(record.metadata.get("mission_task_id")
                                       for record in recent[:10]
                                       if record.metadata.get("mission_task_id")
                                       in snapshot.tasks))[:4]
        self.view = self.reader.overview(feedback_task_ids=feedback)
        self._consume(self.view)
        self._baseline_seen = {kind: set(values) for kind, values in self.seen.items()}

    def _consume(self, result):
        if result.get("status") != "ok":
            return
        for card in result.get("records", []):
            self.seen[card["kind"]].add(card["identity"])

    def _detail(self, kind, identity):
        result = self.reader.query(ContextQuery(
            kind=kind, mode="read", refs=(ContextRef(kind=kind, identity=identity),),
            max_bytes=32768,
        ))
        return result

    def base_fields(self):
        fields: dict[str, Any] = {"investigation_context": self.view,
                                 "candidates": [], "investigations": [],
                                 "execution_history": []}
        # Hydrate a finite frontier automatically: small passes need no lookup.
        # The overview cards are retained only when the exact body cannot fit.
        remaining = 32768
        hydrated = set()
        for card in self.view.get("records", []):
            kind, identity = card["kind"], card["identity"]
            field = {"task": "investigations", "candidate": "candidates",
                     "execution": "execution_history"}.get(kind)
            if field is None:
                continue
            detail = self._detail(kind, identity)
            if detail["status"] != "ok":
                continue
            body = detail["records"][0]["data"]
            size = context_bytes(body)
            if size > remaining:
                continue
            self._consume(detail)
            fields[field].append(body)
            remaining -= size
            hydrated.add((kind, identity))
        # Avoid transmitting full results a second time as summary cards.
        self.view = {**self.view, "records": [
            {"kind": card["kind"], "identity": card["identity"], "view_kind": "detail",
             "field": {"task": "investigations", "candidate": "candidates",
                       "execution": "execution_history"}[card["kind"]]}
            if (card["kind"], card["identity"]) in hydrated else card
            for card in self.view.get("records", [])]}
        fields["investigation_context"] = self.view
        selected = self._related_evidence(fields["investigations"], fields["candidates"])
        frontier = sorted(self.records.values(), key=lambda record: (
            str(record.evidence_id) in selected, record.created_at, str(record.evidence_id)),
            reverse=True)[:10]
        # Receipt windows have their own exact binding filter below.
        frontier = [item for item in frontier if item.metadata.get("record_kind")
                    != "review_receipt"]
        fields.update(
            runtime_records=project_runtime_records(
                frontier, run_id=self.reader.snapshot.run_id,
                engagement_id=self.reader.snapshot.engagement_id, count=6, limit=1200),
            original_observations=project_original_records(
                frontier, run_id=self.reader.snapshot.run_id,
                engagement_id=self.reader.snapshot.engagement_id, count=4, total_limit=4000),
        )
        shown = {item["evidence_id"] for key in ("runtime_records", "original_observations")
                 for item in fields[key]["records"]}
        receipts = self._receipts({item["hypothesis_id"] for item in fields["candidates"]})
        fields["review_receipts"] = receipts
        shown.update(item["evidence_id"] for item in receipts)
        fields["evidence_index"] = sorted(shown)
        fields["evidence_summary"] = {"total": len(self.records), "shown": len(shown),
                                      "omitted_count": len(self.records) - len(shown)}
        fields["evidence_validity"] = [self.validity[ref] for ref in sorted(shown)
                                       if ref in self.validity]
        # Diagnostic/original windows own evidence presentation, not full context.evidence.
        fields["context_lookup"] = {
            "version": 1, "snapshot_id": self.reader.snapshot_id,
            "note": "Optional context_request replaces a decision; same normal budget. "
                    "Version 2 can read up to four independent queries in one request. Recent "
                    "lookup windows are retained within the request envelope; summaries are "
                    "not full reads. "
                    "List/related finds omitted IDs, read fetches exact sections. "
                    "No match is scoped, not proof of no duplicates. Evidence supports "
                    "task_id/hypothesis_id/record_kind filters and bounded windows.",
        }
        return fields

    @staticmethod
    def _related_evidence(tasks, candidates):
        refs = {ref for item in tasks for ref in [*item.get("evidence_ids", []),
                *(item.get("result") or {}).get("evidence_ids", [])]}
        refs.update(ref for item in candidates for ref in (
            *item.get("supporting_evidence_ids", []), *item.get("refuting_evidence_ids", [])))
        return refs

    def _receipts(self, identities):
        candidates = self.canonical.candidates
        matching = []
        for record in self.records.values():
            meta = record.metadata
            identity = meta.get("hypothesis_id")
            if (identity not in identities or identity not in candidates
                    or meta.get("record_kind") != "review_receipt"
                    or meta.get("public") is not True or meta.get("admission") != "final"):
                continue
            candidate = candidates[identity]
            if (meta.get("candidate_revision") != candidate.revision
                    or meta.get("candidate_purpose_hash") != content_hash(
                        hypothesis_purpose(candidate))):
                continue
            matching.append(record)
        # Only the public contract is included; unrelated/private metadata is never dumped.
        result = []
        for record in sorted(matching, key=lambda record: record.created_at, reverse=True)[:4]:
            meta = public_receipt_metadata(record.metadata)
            item = {"evidence_id": str(record.evidence_id), "metadata": meta}
            if context_bytes([*result, item]) <= 8192:
                result.append(item)
        return result

    def validate_reviewed(self, decision):
        if set(decision.reviewed_task_ids) - self.seen["task"]:
            raise PlannerReviewedReferenceError("reviewed_task_ids")
        if set(decision.reviewed_hypothesis_ids) - self.seen["candidate"]:
            raise PlannerReviewedReferenceError("reviewed_hypothesis_ids")

    async def lookup(self, request: PlannerLookupRequest, *, before_each=None):
        if request.version == 2:
            items = request.queries
            assert items is not None
            results = []
            visible: set[tuple[str, str]] = set()
            for item in items:
                # Each item runs against the same immutable snapshot. Errors are item-local.
                if before_each is not None:
                    await before_each()
                result, item_visible = await self._lookup_item(item)
                visible.update(item_visible)
                results.append({"query": item.model_dump(mode="json"), "result": result})
            return {"version": 2, "status": "ok", "results": results}, visible
        if before_each is not None:
            await before_each()
        result, visible = await self._lookup_item(request)
        return result, visible

    async def _lookup_item(self, request: PlannerContextBatchItem | PlannerContextRequest):
        query: ContextQuery | PlannerEvidenceQuery | PlannerContextRequest | PlannerContextBatchItem
        if isinstance(request, PlannerContextBatchItem):
            query = (request.context if request.context is not None else
                     request.evidence if request.evidence is not None else request)
            key = content_hash(request.model_dump(mode="json"))
        else:
            query = (request.context if request.context is not None else
                     request.evidence if request.evidence is not None else request)
            key = content_hash(request.model_dump(mode="json"))
        if key in self.cache:
            return self.cache[key], self._visible_for(query, self.cache[key])
        if isinstance(query, ContextQuery):
            result = self.reader.query(query)
            if result.get("status") == "ok":
                for ref in result.get("relation_read_refs", []):
                    self.consumed[ref["kind"]].add(ref["identity"])
                for card in result.get("records", []):
                    self.consumed[card["kind"]].add(card["identity"])
                for ref in query.refs:
                    self.consumed[ref.kind].add(ref.identity)
                self.consumed["node"].update(query.target_node_ids)
                self.consumed["candidate"].update(query.hypothesis_revisions)
        else:
            result = await self._evidence_lookup(query)
        result = redact_secret_material(result)
        self.cache[key] = result
        return result, self._visible_for(query, result)

    @staticmethod
    def _visible_for(query, result):
        visible = set()
        if isinstance(query, ContextQuery) and result.get("status") == "ok":
            visible.update((card["kind"], card["identity"])
                           for card in result.get("records", []))
            visible.update((ref["kind"], ref["identity"])
                           for ref in result.get("relation_read_refs", []))
        elif isinstance(query, PlannerEvidenceQuery) and query.mode == "read" \
                and result.get("status") == "ok":
            visible.add(("evidence", query.evidence_id))
        return visible

    def retain_lookup_window(self, payload, request, result, visible):
        """Keep recent windows only while the complete next request fits its hard cap."""
        public = {"request": request, "result": result,
                  "retention": "Recent bounded lookup windows are retained; omitted windows "
                               "remain readable by querying again."}
        entry = {"request": request, "result": result, "public": public, "_visible": visible}
        windows = [*self.recent_windows, entry]
        # Bounded recent-window policy; also evict until serialized request can fit.
        windows = windows[-8:]
        while windows:
            all_v1_candidate = all(item["request"].get("version") == 1 for item in windows)
            if all_v1_candidate:
                shaped = {"context_lookup_result": windows[-1]["public"],
                          "context_lookup_history": [item["public"] for item in windows[:-1]]}
            else:
                shaped = {"context_lookup_results": [
                    {"request": item["request"], "result": item["result"]} for item in windows]}
            if context_bytes(shaped) <= 65536:
                break
            windows.pop(0)
        if not windows:
            return False
        # Preserve the exact v1 shape for existing readers when only v1 lookups are retained.
        all_v1 = all(item["request"].get("version") == 1 for item in windows)
        if all_v1:
            candidate = {"context_lookup_result": windows[-1]["public"],
                         "context_lookup_history": [item["public"] for item in windows[:-1]]}
        else:
            candidate = {"context_lookup_results": [
                {"request": item["request"], "result": item["result"]} for item in windows]}
        if context_bytes({**payload, **candidate}) > 131072:
            return False
        self.recent_windows = windows
        self._retained_all_v1 = all_v1
        # Visibility for reviewed IDs follows retained windows exactly. Preserve the captured
        # base view and replace prior lookup visibility when its windows are evicted.
        self._recent_seen = [item["_visible"] for item in windows]
        next_lookup_visible = set().union(*self._recent_seen) if self._recent_seen else set()
        for kind in self.seen:
            self.seen[kind].difference_update(identity for old_kind, identity
                                              in self._lookup_visible if old_kind == kind)
            self.seen[kind].update(self._baseline_seen[kind])
        for seen in self._recent_seen:
            for kind, identity in seen:
                if kind in self.seen:
                    self.seen[kind].add(identity)
        self._lookup_visible = next_lookup_visible
        self.used_evidence.update(identity for kind, identity in next_lookup_visible
                                  if kind == "evidence")
        return True

    def _retained_lookup_payload(self, payload, request):
        windows = self.recent_windows
        if not windows:
            return {}
        if not getattr(self, "_retained_all_v1", False):
            return {"context_lookup_results": [
                {"request": item["request"], "result": item["result"]} for item in windows]}
        return {"context_lookup_result": windows[-1]["public"],
                "context_lookup_history": [item["public"] for item in windows[:-1]]}

    def batch_budget_result(self, request):
        """Explicitly tell the model to narrow reads without retaining unseen refs."""
        return {"version": request.version, "status": "error",
                "code": "context_batch_budget_exceeded",
                "summary": "Lookup results exceed the remaining 65536-byte lookup window "
                           "budget. No references from these results were admitted for review; "
                           "narrow the queries or read fewer records."}

    async def _evidence_lookup(self, query):
        base = {"snapshot_id": self.reader.snapshot_id, "version": 1}
        if query.snapshot_id and query.snapshot_id != self.reader.snapshot_id:
            return {**base, "status": "error", "code": "context_snapshot_changed"}
        matching = [record for record in self.records.values()
                    if (not query.task_id
                        or record.metadata.get("mission_task_id") == query.task_id)
                    and (not query.hypothesis_id or str(record.hypothesis_id or "")
                         == query.hypothesis_id or record.metadata.get("hypothesis_id")
                         == query.hypothesis_id)
                    and (not query.record_kind or record.metadata.get("record_kind")
                         == query.record_kind)]
        if query.mode == "list":
            matching.sort(key=lambda record: (record.created_at, str(record.evidence_id)))
            items = []
            for record in matching[query.offset:query.offset + query.count]:
                item = {"evidence_id": str(record.evidence_id),
                        "record_kind": record.metadata.get("record_kind"),
                        "task_id": record.metadata.get("mission_task_id"),
                        "hypothesis_id": str(record.hypothesis_id or "") or None}
                if context_bytes([*items, item]) > 8192:
                    break
                items.append(item)
            return {**base, "status": "ok", "records": items, "count": len(matching),
                    "omitted_count": len(matching) - len(items),
                    "next_offset": query.offset + len(items)
                    if query.offset + len(items) < len(matching) else None,
                    "note": "Matching captured records only; listing does not read bodies."}
        record = self.records.get(query.evidence_id)
        if record is None or record not in matching:
            return {**base, "status": "error", "code": "context_evidence_not_in_snapshot"}
        current = await self.store.get(query.evidence_id)
        if current is None or content_hash(current) != content_hash(record):
            return {**base, "status": "error", "code": "context_evidence_changed"}
        result = await EvidenceReader(_PublicReceiptRead(self.store)).read(
            evidence_id=query.evidence_id, run_id=self.reader.snapshot.run_id,
            engagement_id=self.reader.snapshot.engagement_id, offset=query.offset,
            limit=query.limit, query=query.query,
        )
        from redteam.orchestration.evidence_validity import current_validity, is_binding

        validity = await current_validity(
            self.store, self.graph, self.reader.snapshot.run_id, query.evidence_id,
            records=list(self.records.values()),
        )
        for binding in self.records.values():
            if (is_binding(binding)
                    and binding.metadata.get("claim_evidence_id") == query.evidence_id):
                self.consumed["node"].update(
                    item["node_id"] for item in binding.metadata.get("conditions", []))
        result = {**base, **result, "validity": validity.model_dump(mode="json"),
                  "validity_scope": "Captured bindings evaluated against current conditions."}
        if context_bytes(result) > 32768:
            return {**base, "status": "error", "code": "context_evidence_window_too_large"}
        return result

    def read_dependencies(self):
        tasks = set(self.consumed["task"])
        nodes = set(self.consumed["node"])
        nodes.update("hypothesis:" + identity for identity in self.consumed["candidate"])
        for identity in self.used_evidence:
            task_id = self.records[identity].metadata.get("mission_task_id")
            if task_id in self.canonical.tasks:
                tasks.add(task_id)
        for execution in self.canonical.executions:
            if str(execution.invocation_id) in self.consumed["execution"]:
                tasks.add(str(execution.task_id))
        return ({identity: content_hash(self.canonical.nodes.get(identity)) for identity in nodes},
                {identity: content_hash(self.canonical.tasks[identity]) for identity in tasks})

    def auxiliary_reads(self):
        return {
            "context_execution_read": {str(item.invocation_id): content_hash(item)
                                       for item in self.canonical.executions
                                       if str(item.invocation_id) in self.consumed["execution"]},
            "context_edge_read": {str(item.id): content_hash(item) for item in self.canonical.edges
                                  if str(item.id) in self.consumed["edge"]},
        }

    def handoff(self, questions, updates):
        """Hydrate exact related originals, independent of the last lookup window."""
        task_ids = {q.source_task_id for q in questions if q.source_task_id}
        # Source/dependency ancestry is part of the original purpose, not new work.
        pending = list(task_ids)
        while pending:
            task = self.canonical.tasks.get(pending.pop())
            if task is None:
                continue
            for ref in [task.source_task_id, *task.dependency_task_ids]:
                if ref and ref not in task_ids:
                    task_ids.add(ref)
                    pending.append(ref)
            if len(task_ids) > 32:
                raise ContextReadError("atomizer_source_ancestry_too_large")
        candidates = {ref for q in questions for ref in q.hypothesis_revisions}
        candidates.update(update.hypothesis.hypothesis_id for update in updates)
        candidates.update(ref for update in updates
                          for ref in update.hypothesis.source_hypothesis_ids)
        targets = {ref for q in questions for ref in q.target_node_ids}
        related_tasks = [task for task in self.canonical.tasks.values()
                         if str(task.task_id) in task_ids]
        tasks = [task.model_dump(mode="json") for task in related_tasks]
        candidate_bodies = [self.canonical.candidates[ref].model_dump(mode="json")
                            for ref in sorted(candidates) if ref in self.canonical.candidates]
        material = {"investigations": tasks, "candidates": candidate_bodies,
                    "execution_history": [item.model_dump(mode="json")
                                          for item in self.canonical.executions
                                          if str(item.task_id) in {
                                              str(task.task_id) for task in related_tasks}]}
        # Unrelated full contracts are unnecessary for decomposition. Relevant
        # parallel work stays a bounded conflict/wait view, with omissions explicit.
        related_work = self.reader.query(ContextQuery(
            kind="task", target_node_ids=tuple(sorted(targets)[:16]),
            statuses=("pending", "ready", "running", "blocked"), count=4, max_bytes=16384,
        ))
        related_executions = self.reader.query(ContextQuery(
            kind="execution", target_node_ids=tuple(sorted(targets)[:16]),
            count=4, max_bytes=16384,
        ))
        material["related_work"] = related_work
        material["related_executions"] = related_executions
        for page in (related_work, related_executions):
            if page.get("status") == "ok":
                for card in page["records"]:
                    self.consumed[card["kind"]].add(card["identity"])
        refs = self._related_evidence(tasks, candidate_bodies)
        refs.update(ref for question in questions for ref in question.evidence_ids)
        originals = [self.records[ref] for ref in sorted(refs) if ref in self.records]
        material["original_observations"] = project_original_records(
            originals, run_id=self.reader.snapshot.run_id,
            engagement_id=self.reader.snapshot.engagement_id, count=4, total_limit=4000,
        )
        material["evidence_validity"] = [self.validity[ref] for ref in sorted(refs)
                                         if ref in self.validity]
        if context_bytes(material) > 65536:
            raise ContextReadError("atomizer_required_context_too_large")
        self.seen["task"].update(str(task.task_id) for task in related_tasks)
        self.seen["candidate"].update(ref for ref in candidates if ref in self.canonical.candidates)
        execution_ids = {item["invocation_id"] for item in material["execution_history"]}
        self.seen["execution"].update(execution_ids)
        self.consumed["task"].update(str(task.task_id) for task in related_tasks)
        self.consumed["candidate"].update(ref for ref in candidates
                                        if ref in self.canonical.candidates)
        self.consumed["execution"].update(execution_ids)
        return material
