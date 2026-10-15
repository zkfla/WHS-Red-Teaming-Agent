"""Read-only, bounded investigation views over immutable canonical snapshots.

No model/tool dispatch or secondary storage lives here. Planner/native consumers
are wired separately: replacing their full input before lookup is available would
lose information. A snapshot retains full Tasks/candidates/executions for reads;
only explicitly rooted graph neighborhoods are materialized.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.attack_graph.predicates import facts_of
from redteam.core.contracts.execution import TaskExecution
from redteam.core.contracts.graph import GraphEdge, GraphNode, Hypothesis
from redteam.core.contracts.mission import MissionPlan, MissionTask, redact_secret_material
from redteam.core.enums import GraphNodeType, TaskStatus
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.coverage import project_coverage_gaps

if TYPE_CHECKING:
    from redteam.orchestration.central_state import CentralStateStore, CentralTransaction

RecordKind = Literal["task", "candidate", "execution", "node", "edge"]
_KINDS = ("task", "candidate", "execution", "node", "edge")
_TASK_STATES = {item.value for item in TaskStatus}
_CANDIDATE_STATES = {"open", "confirmed", "rejected", "inconclusive", "held"}
_EXECUTION_STATES = {"pending", "running", "succeeded", "failed", "cancelled", "unknown"}
_NOTE = (
    "Bounded untrusted DATA, not execution permission or a verdict. Omitted is not absent. "
    "Search related records then read details; original bodies use EvidenceReader. "
    "Matches identify relationships, not semantic duplicates."
)


_WORKER_ANCESTRY_LIMIT = 64  # Forward source/dependency Tasks one worker may read.
_WORKER_ROOT_LIMIT = 16  # Graph roots per worker capture, within context_neighborhood's cap.


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def context_bytes(value: Any) -> int:
    """UTF-8 JSON bytes, not a provider token or billing estimate."""
    return len(_json(value).encode("utf-8"))


class ContextReadError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ContextRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              hide_input_in_errors=True)
    kind: RecordKind
    identity: str = Field(min_length=1, max_length=512)

    @field_validator("identity")
    @classmethod
    def local_identity(cls, value: str) -> str:
        if not value.strip() or "\x1f" in value or "\x00" in value:
            raise ValueError("context_identity_invalid")
        return value


class ContextQuery(BaseModel):
    """One finite search/list, point read, or relationship traversal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              hide_input_in_errors=True)
    kind: RecordKind = "task"
    mode: Literal["list", "related", "read"] = "list"
    refs: tuple[ContextRef, ...] = Field(default=(), max_length=16)
    target_node_ids: tuple[str, ...] = Field(default=(), max_length=16)
    principal_refs: tuple[str, ...] = Field(default=(), max_length=16)
    hypothesis_revisions: dict[str, int] = Field(default_factory=dict, max_length=16)
    statuses: tuple[str, ...] = Field(default=(), max_length=8)
    text: str | None = Field(default=None, min_length=1, max_length=256)
    section: Literal["record", "completion_criteria", "result", "properties"] = "record"
    hops: int = Field(default=1, ge=0, le=3)
    count: int = Field(default=8, ge=1, le=20)
    max_bytes: int = Field(default=16384, ge=2048, le=65536)
    max_visited: int = Field(default=64, ge=1, le=128)
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    continuation: str | None = Field(default=None, max_length=128)

    @field_validator("refs", "target_node_ids", "principal_refs", "statuses", mode="before")
    @classmethod
    def json_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def query_contract(self):
        identities = [*self.target_node_ids, *self.principal_refs, *self.hypothesis_revisions]
        if any(not value.strip() or len(value) > 512 or "\x1f" in value
               or "\x00" in value for value in identities):
            raise ValueError("context_selector_invalid")
        if any(type(version) is not int or version < 0
               for version in self.hypothesis_revisions.values()):
            raise ValueError("context_revision_invalid")
        states = {"task": _TASK_STATES, "candidate": _CANDIDATE_STATES,
                  "execution": _EXECUTION_STATES}.get(self.kind, set())
        if set(self.statuses) - states:
            raise ValueError("context_status_invalid")
        if self.mode == "related" and not self.refs:
            raise ValueError("context_relation_root_required")
        if self.mode == "list" and self.refs:
            raise ValueError("context_refs_require_related_or_read")
        if self.mode == "read" and (len(self.refs) != 1 or self.refs[0].kind != self.kind):
            raise ValueError("context_read_needs_one_matching_reference")
        if self.mode != "read" and self.section != "record":
            raise ValueError("context_section_requires_read")
        if self.section in {"completion_criteria", "result"} and self.kind != "task":
            raise ValueError("context_task_section_required")
        if self.section == "properties" and self.kind not in {"node", "edge"}:
            raise ValueError("context_graph_section_required")
        return self


@dataclass(frozen=True)
class SnapshotRecord:
    kind: RecordKind
    identity: str
    payload: str
    source_hash: str


@dataclass(frozen=True)
class InvestigationSnapshot:
    """Immutable JSON records: mutating an input model cannot alter captured reads."""

    run_id: str
    engagement_id: str
    mission_id: str
    mission_payload: str
    binding_hash: str
    records: tuple[SnapshotRecord, ...]
    graph_roots: tuple[str, ...] = ()
    graph_truncated: bool = False

    @classmethod
    def create(
        cls, *, run_id: str, engagement_id: str, mission: MissionPlan, binding_hash: str,
        tasks: list[MissionTask], candidates: list[Hypothesis], executions: list[TaskExecution],
        nodes: list[GraphNode] | None = None, edges: list[GraphEdge] | None = None,
        graph_roots: tuple[str, ...] = (), graph_truncated: bool = False,
    ) -> InvestigationSnapshot:
        if not run_id or not engagement_id or str(mission.engagement_id) != engagement_id:
            raise ContextReadError("context_scope_mismatch")
        if any(str(task.mission_id) != str(mission.mission_id) for task in tasks):
            raise ContextReadError("context_task_scope_mismatch")
        task_ids = {str(task.task_id) for task in tasks}
        if any(str(item.run_id) != run_id or str(item.engagement_id) != engagement_id
               or str(item.task_id) not in task_ids for item in executions):
            raise ContextReadError("context_execution_scope_mismatch")
        groups: tuple[tuple[RecordKind, Any, str], ...] = (
                  ("task", tasks, "task_id"), ("candidate", candidates, "hypothesis_id"),
                  ("execution", executions, "invocation_id"), ("node", nodes or [], "id"),
                  ("edge", edges or [], "id"))
        records = []
        for kind, items, field in groups:
            identities = set()
            for item in items:
                identity = str(getattr(item, field))
                ContextRef(kind=kind, identity=identity)
                if identity in identities:
                    raise ContextReadError("context_duplicate_record")
                identities.add(identity)
                records.append(SnapshotRecord(
                    kind, identity, _json(redact_secret_material(item.model_dump(mode="json"))),
                    content_hash(item),
                ))
        return cls(run_id, engagement_id, str(mission.mission_id),
                   _json(redact_secret_material(mission.model_dump(mode="json"))), binding_hash,
                   tuple(sorted(records, key=lambda item: (item.kind, item.identity))),
                   tuple(sorted(set(graph_roots))), graph_truncated)

    @property
    def snapshot_id(self) -> str:
        return content_hash({
            "version": 1, "run_id": self.run_id, "engagement_id": self.engagement_id,
            "mission": self.mission_payload, "binding": self.binding_hash,
            "records": [(item.kind, item.identity, item.source_hash, item.payload)
                        for item in self.records],
            "graph_roots": self.graph_roots, "graph_truncated": self.graph_truncated,
        })


async def capture_investigation_context(
    state: CentralStateStore, *, graph_roots: tuple[str, ...] = (), hops: int = 1,
    max_nodes: int = 32, max_edges: int = 64, worker_task_id: str | None = None,
) -> InvestigationSnapshot:
    """One borrowed read transaction; no writes, models, full graph, or evidence bodies.

    With worker_task_id, roots are that Task's own targets and forward ancestry, read in this
    same transaction. Pass the same id to InvestigationContextReader(worker_task_id=...).
    """
    if worker_task_id is not None and graph_roots:
        raise ValueError("context_worker_roots_are_derived")
    def capture(tx: CentralTransaction) -> InvestigationSnapshot:
        mission_id, binding = tx.binding()
        mission = tx.get_mission(mission_id)
        if mission is None:
            raise ContextReadError("context_mission_missing")
        tasks = tx.list_tasks()
        roots, roots_truncated = graph_roots, False
        if worker_task_id is not None:
            roots, roots_truncated = _worker_graph_roots(tasks, worker_task_id)
        nodes, edges, truncated = tx.graph.context_neighborhood(
            roots, hops=hops, max_nodes=max_nodes, max_edges=max_edges,
        )
        candidates = [Hypothesis.model_validate(facts_of(node.properties))
                      for node in tx.graph.query(node_type=GraphNodeType.HYPOTHESIS)]
        return InvestigationSnapshot.create(
            run_id=tx.run_id, engagement_id=tx.engagement_id, mission=mission,
            binding_hash=binding, tasks=tasks, candidates=candidates,
            executions=tx.list_executions(), nodes=nodes, edges=edges,
            graph_roots=roots, graph_truncated=truncated or roots_truncated,
        )
    return await state.transaction(capture, write=False)


def _worker_ancestry(
    task_id: str, refs_of: Callable[[str], list[str] | None],
) -> tuple[list[str], bool]:
    """Forward source/dependency Tasks from task_id, bounded. refs_of returns None if missing."""
    order: list[str] = []
    seen: set[str] = set()
    queue = deque([task_id])
    while queue and len(order) < _WORKER_ANCESTRY_LIMIT:
        current = queue.popleft()
        if current in seen:
            continue
        refs = refs_of(current)
        if refs is None:
            continue
        seen.add(current)
        order.append(current)
        queue.extend(refs)
    return order, bool(queue)


def _worker_graph_roots(tasks: list[MissionTask], task_id: str) -> tuple[tuple[str, ...], bool]:
    """A worker's related graph targets across its own ancestry, capped for one neighborhood."""
    by_id = {str(task.task_id): task for task in tasks}

    def refs_of(current: str) -> list[str] | None:
        task = by_id.get(current)
        if task is None:
            return None
        return [str(ref) for ref in [task.source_task_id, *task.dependency_task_ids] if ref]

    ids, ancestry_truncated = _worker_ancestry(task_id, refs_of)
    roots = sorted({str(node) for current in ids
                    for node in by_id[current].related_attack_graph_node_ids})
    truncated = ancestry_truncated or len(roots) > _WORKER_ROOT_LIMIT
    return tuple(roots[:_WORKER_ROOT_LIMIT]), truncated


def _preview(value: Any, path: str, cut: list[str], *, depth: int = 0) -> Any:
    """Cards only. Full detail/mandatory completion contracts are never sliced."""
    if depth > 6:
        cut.append(path)
        return None
    if isinstance(value, str):
        # IDs remain exact. A pathological identity that exceeds the envelope
        # is rejected by the page budget rather than silently rewritten.
        if path.endswith(("_id", "_ref", "ids", "refs", "revisions", ".identity")):
            return value
        if len(value) > 400:
            cut.append(path)
            return value[:400]
    elif isinstance(value, list):
        if len(value) > 4:
            cut.append(path)
        return [_preview(item, path, cut, depth=depth + 1) for item in value[:4]]
    elif isinstance(value, dict):
        if len(value) > 8:
            cut.append(path)
        return {key: _preview(item, f"{path}.{key}", cut, depth=depth + 1)
                for key, item in sorted(value.items())[:8]}
    return value


class InvestigationContextReader:
    """Pure views. The caller owns the current snapshot and any version/CAS admission."""

    def __init__(self, snapshot: InvestigationSnapshot, *, worker_task_id: str | None = None):
        self.snapshot = snapshot
        self.snapshot_id = snapshot.snapshot_id
        self._records: dict[tuple[str, str], dict[str, Any]] = {
            (item.kind, item.identity): json.loads(item.payload) for item in snapshot.records
        }
        self._links: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for (kind, identity), item in self._records.items():
            if kind == "task":
                for related in [item.get("source_task_id"), item.get("parent_task_id"),
                                *item.get("dependency_task_ids", [])]:
                    if related:
                        self._link((kind, identity), ("task", related))
                for candidate in item.get("related_hypothesis_ids", []):
                    self._link((kind, identity), ("candidate", candidate))
                for node in item.get("related_attack_graph_node_ids", []):
                    self._link((kind, identity), ("node", node))
            elif kind == "candidate":
                for node in item.get("target_node_ids", []):
                    self._link((kind, identity), ("node", node))
                for candidate in item.get("source_hypothesis_ids", []):
                    self._link((kind, identity), ("candidate", candidate))
            elif kind == "execution":
                self._link((kind, identity), ("task", item["task_id"]))
                if item.get("target_node_id"):
                    self._link((kind, identity), ("node", item["target_node_id"]))
            elif kind == "edge":
                self._link((kind, identity), ("node", item["source_id"]))
                self._link((kind, identity), ("node", item["target_id"]))
        self._allowed = set(self._records)
        self._allowed_roots = set(self._links)
        self.worker_task_id = worker_task_id
        self.worker_scope_truncated = False
        if worker_task_id is not None:
            self._restrict_worker(worker_task_id)

    def _link(self, left, right):
        self._links.setdefault(left, set()).add(right)
        self._links.setdefault(right, set()).add(left)

    def _restrict_worker(self, identity: str) -> None:
        if ("task", identity) not in self._records:
            raise ContextReadError("context_worker_task_missing")
        # Forward source/dependency ancestry only, never reverse sibling tasks.
        def refs(task_id: str) -> list[str]:
            item = self._records.get(("task", task_id))
            if item is None:
                raise ContextReadError("context_worker_source_missing")
            return [ref for ref in [item.get("source_task_id"),
                                    *item.get("dependency_task_ids", [])] if ref]
        ids, self.worker_scope_truncated = _worker_ancestry(identity, refs)
        allowed: set[tuple[str, str]] = {("task", task_id) for task_id in ids}
        for key in list(allowed):
            item = self._records[key]
            allowed.update(("candidate", ref) for ref in item.get("related_hypothesis_ids", []))
            allowed.update(("node", ref) for ref in item.get("related_attack_graph_node_ids", []))
        for key in list(allowed):
            if key[0] == "candidate":
                allowed.update(("node", ref)
                               for ref in self._records.get(key, {}).get("target_node_ids", []))
        task = self._records[("task", identity)]
        if any(self._records.get(("candidate", candidate), {}).get("revision", 0) != version
               for candidate, version in task.get("hypothesis_revisions", {}).items()):
            raise ContextReadError("context_worker_candidate_revision_unavailable")
        allowed.update(key for key, item in self._records.items()
                       if key[0] == "execution" and ("task", item["task_id"]) in allowed)
        allowed.update(key for key, item in self._records.items() if key[0] == "edge"
                       and ("node", item["source_id"]) in allowed
                       and ("node", item["target_id"]) in allowed)
        self._allowed_roots = set(allowed)
        self._allowed = allowed & self._records.keys()

    def _scope(self) -> dict:
        return {"run_id": self.snapshot.run_id, "engagement_id": self.snapshot.engagement_id,
                "mission_id": self.snapshot.mission_id, "worker_task_id": self.worker_task_id}

    def _error(self, code: str, **details) -> dict:
        return {"version": 1, "status": "error", "code": code,
                "snapshot_id": self.snapshot_id, **details}

    def _require(self, key: tuple[str, str]) -> None:
        if key not in self._records and key not in self._links:
            raise ContextReadError("context_reference_not_found")
        if key not in self._allowed and key not in self._allowed_roots:
            raise ContextReadError("context_reference_outside_read_scope")

    def _summary(self) -> dict:
        groups = {kind: [item for key, item in self._records.items()
                         if key in self._allowed and key[0] == kind] for kind in _KINDS}
        def states(kind: str, allowed: set[str]) -> dict:
            return dict(sorted(Counter(item.get("status", item.get("state"))
                                       if item.get("status", item.get("state")) in allowed
                                       else "other" for item in groups[kind]).items()))
        mission = MissionPlan.model_validate_json(self.snapshot.mission_payload)
        tasks = [MissionTask.model_validate(item) for item in groups["task"]]
        coverage = project_coverage_gaps(mission=mission, tasks=tasks)
        unsafe_count = sum(bool(unsafe(TaskExecution.model_validate(item)))
                           for item in groups["execution"])
        return {
            "totals": {kind: len(items) for kind, items in groups.items()},
            "states": {"task": states("task", _TASK_STATES),
                       "candidate": states("candidate", _CANDIDATE_STATES),
                       "execution": states("execution", _EXECUTION_STATES)},
            "unsafe_executions": unsafe_count,
            "waits": dict(sorted(Counter(item["wait_reason"] for item in groups["execution"]
                                         if item.get("wait_reason")).items())),
            "required_incomplete_tasks": len(coverage.required_incomplete_tasks),
            "completed_without_receipt": len(coverage.completed_without_receipt),
            "unmet_goal_conditions": len(coverage.unmet_conditions),
            "goal": mission.objective[:400], "goal_truncated": len(mission.objective) > 400,
            "mission_revision": mission.revision, "mission_status": mission.status.value,
            "graph_neighborhood_truncated": self.snapshot.graph_truncated,
            "worker_scope_truncated": self.worker_scope_truncated,
        }

    def _card(self, key: tuple[str, str], *, reason: str) -> dict:
        kind, identity = key
        item = self._records[key]
        if kind == "task":
            fields = ("title", "objective", "status", "required", "scope_refs", "principal_refs",
                      "related_attack_graph_node_ids", "hypothesis_revisions", "source_task_id",
                      "source_result_id", "source_question_id", "dependency_task_ids",
                      "goal_required_hypothesis_ids", "blocking_reason", "evidence_ids",
                      "completion_evidence_id", "updated_at")
            body = {field: item.get(field) for field in fields}
            result = item.get("result")
            if isinstance(result, dict):
                body["result"] = {field: result.get(field) for field in (
                    "result_id", "outcome", "summary", "observations", "limitations",
                    "evidence_ids", "questions", "hypothesis_ids", "completion_receipt_id",
                )}
            body["completion_criteria_count"] = len(item.get("completion_criteria", []))
        elif kind == "candidate":
            body = {field: item.get(field) for field in (
                "description", "status", "revision", "premise", "reason", "target_node_ids",
                "principal_refs", "supporting_evidence_ids", "refuting_evidence_ids",
                "source_hypothesis_ids",
            )}
        elif kind == "execution":
            body = {field: item.get(field) for field in (
                "task_id", "action_id", "invocation_id", "tool_id", "state", "wait_reason",
                "approval_ref", "job_ref", "resource_execution_pending", "resource_keys",
                "proposal_evidence_id", "result_evidence_id", "target_node_id",
            )}
            body["unsafe"] = bool(unsafe(TaskExecution.model_validate(item)))
            body["recency"] = "unknown"  # Invocation IDs are not timestamps.
        elif kind == "node":
            body = {field: item.get(field) for field in ("type", "labels", "updated_at")}
            body["facts"] = facts_of(item.get("properties", {}))
        else:
            body = {field: item.get(field) for field in ("type", "source_id", "target_id")}
        cut: list[str] = []
        # Preview each top-level field separately so optional-field count never
        # drops status, source links, current waits or exact revision semantics.
        preview = {field: _preview(value, field, cut) for field, value in body.items()}
        return {"kind": kind, "identity": identity, "view_kind": "summary",
                "selection_reason": reason, "data": preview,
                "truncated": bool(cut), "truncated_fields": sorted(set(cut))[:32],
                "detail_available": True}

    def _related(self, refs: tuple[ContextRef, ...], hops: int, max_visited: int):
        roots: set[tuple[str, str]] = {(ref.kind, ref.identity) for ref in refs}
        for key in roots:
            self._require(key)
        if len(roots) > max_visited:
            raise ContextReadError("context_relation_limit_too_small")
        visited = set(roots)
        queue = deque((key, 0) for key in sorted(roots))
        truncated = False
        while queue:
            key, depth = queue.popleft()
            if depth >= hops:
                continue
            for neighbor in sorted(self._links.get(key, ())):
                if neighbor in visited or neighbor not in self._allowed:
                    continue
                if len(visited) >= max_visited:
                    truncated = True
                    break
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
        return visited, truncated

    def _matches(self, key, query: ContextQuery) -> bool:
        item = self._records[key]
        related_task = self._records.get(("task", item.get("task_id", "")), {}) \
            if key[0] == "execution" else {}
        if query.statuses and item.get("status", item.get("state")) not in query.statuses:
            return False
        if query.target_node_ids:
            targets = set(item.get("target_node_ids", [])) | set(
                item.get("related_attack_graph_node_ids", [])) | {item.get("target_node_id")}
            if key[0] == "node":
                targets.add(key[1])
            if key[0] == "edge":
                targets.update((item["source_id"], item["target_id"]))
            if key[0] == "execution":
                targets.update(related_task.get("related_attack_graph_node_ids", []))
            if not targets.intersection(query.target_node_ids):
                return False
        principals = set(item.get("principal_refs", [])) | set(
            related_task.get("principal_refs", []))
        if query.principal_refs and not principals.intersection(query.principal_refs):
            return False
        if query.hypothesis_revisions:
            versions = (item.get("hypothesis_revisions", {}) if key[0] == "task"
                        else {key[1]: item.get("revision")} if key[0] == "candidate"
                        else related_task.get("hypothesis_revisions", {}))
            if not any(versions.get(ref) == revision
                       for ref, revision in query.hypothesis_revisions.items()):
                return False
        if query.text:
            searchable = " ".join(str(item.get(field) or "")
                                  for field in ("title", "objective", "description", "premise"))
            if query.text.casefold() not in searchable.casefold():
                return False
        return True

    def _cursor(self, query: ContextQuery, offset: int) -> str:
        binding = {"snapshot": self.snapshot_id, "scope": self._scope(), "offset": offset,
                   "query": query.model_dump(exclude={"continuation", "snapshot_id"})}
        return f"{offset:x}." + content_hash(binding)

    def _offset(self, query: ContextQuery) -> int:
        if not query.continuation:
            return 0
        try:
            offset = int(query.continuation.split(".", 1)[0], 16)
        except ValueError:
            raise ContextReadError("context_continuation_invalid") from None
        if offset < 0 or query.continuation != self._cursor(query, offset):
            raise ContextReadError("context_continuation_scope_or_snapshot_changed")
        return offset

    def query(self, query: ContextQuery) -> dict:
        """Bound the *entire* response, including summary/cursor and escaped Unicode."""
        if query.snapshot_id is not None and query.snapshot_id != self.snapshot_id:
            return self._error("context_snapshot_changed")
        try:
            offset = self._offset(query)
            for identity in query.target_node_ids:
                self._require(("node", identity))
            if query.principal_refs:
                known_principals = {ref for key in self._allowed
                                    for ref in self._records[key].get("principal_refs", [])}
                if set(query.principal_refs) - known_principals:
                    raise ContextReadError("context_principal_not_in_read_scope")
            for identity, version in query.hypothesis_revisions.items():
                self._require(("candidate", identity))
                if self._records[("candidate", identity)].get("revision", 0) != version:
                    raise ContextReadError("context_candidate_revision_changed")
            truncated = False
            if query.mode == "read":
                key: tuple[str, str] = (query.kind, query.refs[0].identity)
                self._require(key)
                if key not in self._records:
                    raise ContextReadError("context_graph_detail_not_captured")
                if not self._matches(key, query):
                    raise ContextReadError("context_record_filter_mismatch")
                keys: list[tuple[str, str]] = [key]
            else:
                related = self._allowed
                if query.mode == "related":
                    related, truncated = self._related(query.refs, query.hops, query.max_visited)
                keys = sorted(key for key in self._allowed if key[0] == query.kind
                              and key in related and self._matches(key, query))
            if offset > len(keys):
                raise ContextReadError("context_continuation_invalid")
            response: dict[str, Any] = {
                "version": 1, "status": "ok", "snapshot_id": self.snapshot_id,
                "scope": self._scope(), "view_kind": "detail" if query.mode == "read"
                else "summary", "kind": query.kind, "summary": self._summary(),
                "records": [], "count": len(keys), "selected_count": 0,
                "omitted_count": len(keys), "relation_truncated": truncated,
                "count_scope": "matching_captured_records",
                "offset": offset, "continuation": None, "note": _NOTE,
                "relation_read_refs": [{"kind": key[0], "identity": key[1]}
                                       for key in sorted(related)]
                if query.mode == "related" else [],
            }
            for key in keys[offset:offset + query.count]:
                if query.mode == "read":
                    item = self._records[key]
                    body = item if query.section == "record" else item.get(query.section)
                    card = {"kind": key[0], "identity": key[1], "view_kind": "detail",
                            "section": query.section, "data": json.loads(_json(body)),
                            "truncated": False}
                else:
                    card = self._card(key, reason="related" if query.mode == "related"
                                      else "filtered" if (query.text or query.statuses
                                                          or query.target_node_ids
                                                          or query.principal_refs
                                                          or query.hypothesis_revisions)
                                      else "identity_order_not_recent")
                trial = {**response, "records": [*response["records"], card]}
                selected = len(trial["records"])
                next_offset = offset + selected
                trial.update(selected_count=selected, omitted_count=len(keys) - selected,
                             continuation=self._cursor(query, next_offset)
                             if next_offset < len(keys) else None)
                if context_bytes(trial) > query.max_bytes:
                    if not response["records"]:
                        return self._error("context_detail_too_large" if query.mode == "read"
                                           else "context_card_too_large",
                                           required_bytes=context_bytes(trial),
                                           max_bytes=query.max_bytes,
                                           repair="Read a smaller section or increase max_bytes.")
                    break
                response = trial
            if context_bytes(response) > query.max_bytes:
                return self._error("context_summary_too_large", max_bytes=query.max_bytes)
            return response
        except ContextReadError as exc:
            return self._error(exc.code)

    def overview(
        self, *, active_task_id: str | None = None, feedback_task_ids: tuple[str, ...] = (),
        target_node_ids: tuple[str, ...] = (), count: int = 4, max_bytes: int = 24576,
    ) -> dict:
        """Finite frontier cards plus the active Task's exact mandatory contract."""
        active_task_id = active_task_id or self.worker_task_id
        # Use the same validated count/byte/identity contract as point queries.
        ContextQuery(count=count, max_bytes=max_bytes, target_node_ids=target_node_ids)
        if len(feedback_task_ids) > 16:
            return self._error("context_feedback_limit_exceeded")
        active = ("task", active_task_id) if active_task_id else None
        try:
            for identity in (*feedback_task_ids, *((active_task_id,) if active_task_id else ())):
                self._require(("task", identity))
        except ContextReadError as exc:
            return self._error(exc.code)
        focus = set(feedback_task_ids) | ({active_task_id} if active_task_id else set())
        relevant = set()
        focus_candidates = set()
        focus_targets = set(target_node_ids)
        for identity in focus:
            task = self._records[("task", identity)]
            relevant.update(task.get("dependency_task_ids", []))
            focus_candidates.update(task.get("related_hypothesis_ids", []))
            focus_targets.update(task.get("related_attack_graph_node_ids", []))
            if task.get("source_task_id"):
                relevant.add(task["source_task_id"])
        response: dict[str, Any] = {"version": 1, "status": "ok", "snapshot_id": self.snapshot_id,
                    "scope": self._scope(), "summary": self._summary(), "records": [],
                    "selected_counts": {kind: 0 for kind in _KINDS}, "note": _NOTE}
        if active is not None:
            item = self._records[active]
            response["active_contract"] = json.loads(_json({field: item.get(field) for field in (
                "task_id", "objective", "completion_contract_version", "completion_criteria",
                "principal_refs", "hypothesis_revisions", "required", "scope_refs",
                "related_attack_graph_node_ids", "evidence_ids",
                "goal_required_hypothesis_ids", "probe_limits", "source_task_id",
                "source_result_id", "source_question_id",
            )}))
            if context_bytes(response) > max_bytes:
                return self._error("context_required_contract_too_large",
                                   required_bytes=context_bytes(response), max_bytes=max_bytes)
        def ranking(key):
            kind, identity = key
            item = self._records[key]
            if key == active:
                rank, reason = 0, "active_task"
            elif kind == "task" and identity in focus:
                rank, reason = 1, "feedback_task"
            elif kind == "task" and identity in relevant:
                rank, reason = 2, "source_or_dependency"
            elif kind == "candidate" and identity in focus_candidates:
                rank, reason = 2, "task_candidate"
            elif kind == "execution" and item["task_id"] in focus:
                rank, reason = 3, "task_execution"
            elif kind == "execution" and unsafe(TaskExecution.model_validate(item)):
                rank, reason = 3, "current_wait_or_uncertainty"
            elif focus_targets.intersection(item.get("target_node_ids", [])
                    or item.get("related_attack_graph_node_ids", [])):
                rank, reason = 4, "selected_target"
            elif kind == "task" and item.get("required") and item["status"] != "completed":
                rank, reason = 5, "required_gap"
            elif kind == "task" and item["status"] in {"pending", "ready", "running", "blocked"}:
                rank, reason = 6, "unfinished_task"
            elif kind == "candidate" and item["status"] in {"open", "held", "inconclusive"}:
                rank, reason = 6, "unresolved_candidate"
            else:
                rank, reason = 7, "historical"
            updated = item.get("updated_at")
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp() \
                if updated else 0
            return rank, -timestamp, identity, reason
        ranked = sorted(self._allowed, key=ranking)
        for key in ranked:
            kind = key[0]
            if response["selected_counts"][kind] >= count:
                continue
            card = self._card(key, reason=ranking(key)[3])
            trial = {**response, "records": [*response["records"], card],
                     "selected_counts": {**response["selected_counts"],
                                         kind: response["selected_counts"][kind] + 1}}
            # Reserve room for bounded omitted-count diagnostics.
            if context_bytes(trial) + 512 <= max_bytes:
                response = trial
        response["omitted_counts"] = {
            kind: response["summary"]["totals"][kind] - response["selected_counts"][kind]
            for kind in _KINDS
        }
        selected_unsafe = sum(card["data"].get("unsafe") is True for card in response["records"])
        response["unsafe_executions_omitted"] = (
            response["summary"]["unsafe_executions"] - selected_unsafe
        )
        if context_bytes(response) > max_bytes:
            return self._error("context_summary_too_large", max_bytes=max_bytes)
        return response
