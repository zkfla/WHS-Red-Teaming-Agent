"""Explicit offline legacy import, retained provenance and guarded resume.

No source setup, provider entry, default selection or dual writes. Every source
row receives a hash and disposition; unsupported bodies stay in their original
files. A migration is committed in one transaction, so an interrupted attempt
has no partly imported Tasks, budgets or receipts to replay.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from redteam.attack_graph.snapshot import from_records
from redteam.attack_graph.stores.central import GraphTransaction
from redteam.attack_graph.stores.mutations import GraphMutationConflict, GraphMutationReceipt
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.events import EventEnvelope
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.contracts.tool import ToolInvocation, execution_outcome_uncertain
from redteam.core.convert import execution_input_digest
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import RunId
from redteam.core.ports.control_snapshot import ControlSnapshotInspection
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.tool_binding import execution_fingerprint
from redteam.orchestration.central_state import (
    CentralStateStore,
    CentralTransaction,
    CheckedEvidence,
    MigrationPermit,
    check_evidence,
    check_execution_receipt,
    execution_identity_hash,
)
from redteam.orchestration.checkpointing import open_identity_reader
from redteam.orchestration.component_binding import RuntimeComponentBinding
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.execution_journal import checked_receipt, preserved_result
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.operator_intents.store import check_intent_update, sanitize_intent_record
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.state_backend import inspect_sqlite_state, run_storage_ownership
from redteam.orchestration.storage_snapshot import OfflineSnapshot, digest, source_manifest
from redteam.orchestration.task_activation import TaskActivation
from redteam.orchestration.work.models import TaskLease, WorkItem, WorkStatus, work_owner_for
from redteam.sqlite_support import read_storage_retirement, retire_storage, run_sqlite

_TARGET_KEYS = {
    "state_missions": ("mission_id",),
    "state_runs": ("run_id",),
    "state_tasks": ("run_id", "task_id"),
    "state_activations": ("run_id", "instance_id"),
    "state_executions": ("run_id", "invocation_id"),
    "state_requests": ("run_id", "kind", "identity"),
    "state_intents": ("run_id", "intent_id"),
    "state_outbox": ("run_id", "event_id"),
    "state_graph_nodes": ("namespace", "node_id"),
    "state_graph_edges": ("namespace", "edge_id"),
    "state_graph_mutations": ("namespace", "run_id", "mutation_id"),
    "network_pools": ("engagement", "purpose"),
    "network_windows": ("window_id",),
    "network_seq_log": ("window_id", "seq"),
}

_SUPPORTED_RECORD_KINDS = {
    "binding",
    "planning_inputs",
    "admission",
    "generation",
    "strategy_revision",
    "task_activation",
    "task_activation_clock",
    "task_activation_policy",
    "task_activation_denial",
    "intent",
    "outbox",
    "outbox_sequence",
    "llm_request",
    "llm_request_policy",
    "llm_provider_cooldown",
    "planning_reference",
    "planning_writer",
    "evidence_validity_projection",
}


@dataclass(frozen=True)
class MigrationRequest:
    migration_id: str
    target: Path
    # Names are source schema owners, not runtime backend/role selections.
    databases: Mapping[str, str | Path]
    references: Mapping[str, str | Path]
    # Original specialist_planning_inputs, checked against the saved hash.
    inputs_by_run: Mapping[str, dict] = field(default_factory=dict)
    offline: bool = False


@dataclass
class _Run:
    run_id: str
    binding: dict
    identity: dict = field(default_factory=dict)
    reasons: set[str] = field(default_factory=set)
    mission: MissionPlan | None = None
    engagement: Engagement | None = None
    tasks: dict[str, MissionTask] = field(default_factory=dict)
    activations: dict[str, dict] = field(default_factory=dict)
    executions: list[TaskExecution] = field(default_factory=list)
    evidence: tuple[CheckedEvidence, ...] = ()


class _Import:
    def __init__(self, snapshot: OfflineSnapshot, request: MigrationRequest,
                 control: ControlSnapshotInspection | None):
        self.snapshot, self.request = snapshot, request
        self.control = control
        self.rows: dict[tuple[str, str, str], dict] = {}
        self.mapping: dict[tuple[str, str, str], dict] = {}
        self.writes: list[tuple[str, dict]] = []
        self.runs: dict[str, _Run] = {}
        self.table_counts: dict[str, dict[str, int]] = {}
        for source in snapshot.connections:
            self.table_counts[source] = {}
            for table in snapshot.tables(source):
                rows = snapshot.rows(source, table)
                self.table_counts[source][table] = len(rows)
                for row in rows:
                    key = (source, table, snapshot.row_key(source, table, row))
                    self.rows[key] = row
                    self.mapping[key] = {
                        "source_hash": digest(row),
                        "disposition": "retained",
                        "reason": "historical_or_unused",
                        "target_table": None,
                        "target_key": None,
                        "target_hash": None,
                    }

    def mark(self, source, table, row, *, reason, target=None, values=None):
        key = (source, table, self.snapshot.row_key(source, table, row))
        item = self.mapping[key]
        item.update(
            reason=reason,
            disposition="active" if target else "retained",
            target_table=None,
            target_key=None,
            target_hash=None,
        )
        if target:
            keys = _TARGET_KEYS[target]
            item.update(
                target_table=target,
                target_key=json.dumps({key: values[key] for key in keys}),
                target_hash=digest(values),
            )
            self.writes.append((target, values))

    def refuse(self, reason: str, run: str | None = None):
        for current in [self.runs[run]] if run in self.runs else self.runs.values():
            current.reasons.add(reason)

    def records(self) -> dict[tuple[str, str], Any]:
        found = {}
        for row in self.snapshot.rows("task", "orchestration_records"):
            try:
                found[(row["kind"], row["identity"])] = json.loads(row["payload"])
            except (ValueError, TypeError):
                self.mark("task", "orchestration_records", row, reason="unreadable_record")
                self.refuse("unreadable_orchestration_record")
        return found


def _prepare_runs(plan: _Import) -> dict[tuple[str, str], Any]:
    records = plan.records()
    for (kind, identity), binding in records.items():
        if kind == "binding" and isinstance(binding, dict):
            plan.runs[identity] = _Run(identity, binding)
    # Existing Work Run IDs are recovery inventory, not newly created Runs.
    # Missing Mission bindings stay refused; unknown shared resources still hold.
    for row in plan.snapshot.rows("work", "work_items"):
        work_run_id = row.get("run_id")
        if isinstance(work_run_id, str) and work_run_id.strip() and work_run_id not in plan.runs:
            plan.runs[work_run_id] = _Run(
                work_run_id, {}, reasons={"original_run_binding_unverified"}
            )
    versions = plan.snapshot.rows("task", "orchestration_migrations")
    if not versions or max(row["version"] for row in versions) != 1:
        plan.refuse("unsupported_orchestration_schema")
    # Replay the unreadable-row diagnostic after the Run inventory is known.
    if any(m["reason"] == "unreadable_record" for m in plan.mapping.values()):
        plan.refuse("unreadable_orchestration_record")
    missions = {}
    for row in plan.snapshot.rows("task", "missions"):
        try:
            mission = MissionPlan.model_validate_json(row["payload"]).redacted_copy()
            if (str(mission.mission_id), str(mission.engagement_id)) != (
                row["mission_id"],
                row["engagement_id"],
            ):
                raise ValueError("mission_index")
            missions[row["mission_id"]] = (mission, row)
        except (ValueError, TypeError):
            plan.mark("task", "missions", row, reason="unreadable_mission")
            plan.refuse("unreadable_mission")
    for run in plan.runs.values():
        run.identity = {"original_binding": run.binding, "checkpoints": [], "approvals": []}
        held = set()
        for row in plan.snapshot.rows("work", "work_items"):
            if row.get("run_id") != run.run_id:
                continue
            try:
                item = WorkItem.model_validate_json(row["payload"])
                if item.resource_execution_pending or item.job_id:
                    held.update(item.resource_keys)
            except (ValueError, TypeError, KeyError):
                run.reasons.add("work_record_unreadable")
        run.identity["held_resource_keys"] = sorted(held)
        mission_row = missions.get(run.binding.get("mission_id"))
        if mission_row is None:
            run.reasons.add("original_mission_missing")
            continue
        run.mission = mission_row[0]
        if str(run.mission.engagement_id) != run.binding.get("engagement_id"):
            run.reasons.add("original_engagement_mismatch")
            continue
        # A mission remains meaningful even if its Run cannot safely resume.
        plan.mark(
            "task",
            "missions",
            mission_row[1],
            reason="original_mission",
            target="state_missions",
            values={
                "mission_id": str(run.mission.mission_id),
                "engagement_id": str(run.mission.engagement_id),
                "payload": run.mission.model_dump_json(),
            },
        )
        inputs = plan.request.inputs_by_run.get(run.run_id)
        saved = records.get(("planning_inputs", run.run_id))
        try:
            if not inputs or inputs.get("schema_version") != 2 or saved != content_hash(inputs):
                raise ValueError("original_inputs")
            # Assembly timestamps are deliberately outside this input binding.
            engagement = Engagement.model_validate(
                {**inputs["engagement"], "created_at": run.mission.created_at}
            )
            components = RuntimeComponentBinding.model_validate(inputs["components"])
            policy = LLMRequestPolicy.model_validate(records[("llm_request_policy", run.run_id)])
            if str(engagement.engagement_id) != run.binding["engagement_id"]:
                raise ValueError("engagement")
            run.engagement = engagement
            run.identity.update(
                input_binding_hash=saved,
                components=components.model_dump(mode="json"),
                llm_request_policy=policy.model_dump(mode="json"),
            )
            plan.writes.append(
                (
                    "state_runs",
                    {
                        "run_id": run.run_id,
                        "engagement_id": run.binding["engagement_id"],
                        "mission_id": run.binding["mission_id"],
                        "scope_json": engagement.scope.model_dump_json(),
                        "binding_hash": saved,
                    },
                )
            )
        except (ValueError, TypeError, KeyError):
            run.reasons.add("original_inputs_components_or_policy_unverified")
        if run.mission.status in {
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.CANCELLED,
        } or run.binding.get("closed"):
            run.reasons.add("original_run_closed")
    return records


def _prepare_tasks(plan: _Import) -> None:
    by_mission = {run.binding.get("mission_id"): run for run in plan.runs.values()}
    for row in plan.snapshot.rows("task", "mission_tasks"):
        run = by_mission.get(row["mission_id"])
        try:
            task = MissionTask.model_validate_json(row["payload"]).redacted_copy()
            if (str(task.task_id), str(task.mission_id), task.status.value, task.priority) != (
                row["task_id"],
                row["mission_id"],
                row["status"],
                row["priority"],
            ):
                raise ValueError("task_index")
            if run is None or run.engagement is None:
                plan.mark("task", "mission_tasks", row, reason="unverified_run_binding")
                continue
            # Keep the original purpose contract hash in receipts. Scheduling
            # fields are removed only from the active record; originals retained.
            if task.status is TaskStatus.RUNNING and not task.occupant_owner:
                run.reasons.add("original_task_owner_missing")
            run.tasks[str(task.task_id)] = task
            active = task.model_copy(
                update={"priority": 0, "parent_task_id": None, "dependency_task_ids": []}
            )
            plan.mark(
                "task",
                "mission_tasks",
                row,
                reason="investigation_purpose",
                target="state_tasks",
                values={
                    "run_id": run.run_id,
                    "task_id": str(task.task_id),
                    "payload": active.model_dump_json(),
                },
            )
        except (ValueError, TypeError, KeyError):
            plan.mark("task", "mission_tasks", row, reason="unreadable_task")
            plan.refuse("unreadable_task", None if run is None else run.run_id)
    for run in plan.runs.values():
        for task in run.tasks.values():
            if task.source_task_id:
                source = run.tasks.get(str(task.source_task_id))
                result = None if source is None else source.result
                if (
                    result is None
                    or result.result_id != task.source_result_id
                    or not any(q.question_id == task.source_question_id for q in result.questions)
                ):
                    run.reasons.add("original_result_lineage_missing")


def _prepare_records(plan: _Import, records: dict) -> None:
    for row in plan.snapshot.rows("task", "orchestration_records"):
        kind, identity = row["kind"], row["identity"]
        value = records.get((kind, identity))
        if value is None:
            continue
        if kind not in _SUPPORTED_RECORD_KINDS:
            plan.mark("task", "orchestration_records", row, reason="unsupported_record_kind")
            plan.refuse("unsupported_orchestration_record_kind")
            continue
        if kind == "llm_request":
            try:
                RunRequestLedger.checked_record(identity, value)
            except CoordinationError:
                plan.mark("task", "orchestration_records", row, reason="request_scope_unverified")
                plan.refuse("original_request_scope_unverified")
                continue
        run_id = value.get("run_id") if isinstance(value, dict) else None
        if kind in {
            "binding",
            "planning_inputs",
            "llm_request_policy",
            "llm_provider_cooldown",
            "outbox_sequence",
        }:
            run_id = identity
        elif kind == "outbox" and isinstance(value, dict):
            envelope = value.get("envelope")
            run_id = envelope.get("run_id") if isinstance(envelope, dict) else None
        run = plan.runs.get(run_id) if isinstance(run_id, str) else None
        if run is None or run.engagement is None:
            continue
        try:
            if kind == "task_activation":
                runtime = RuntimeState.model_validate(value["runtime"])
                task = run.tasks[value["task_id"]]
                stage = value.get("launch_stage")
                if (
                    stage not in {"reserved", "entered", "returned"}
                    or value.get("launch_proof_version") != 1
                    or identity != f"{run.run_id}:{task.task_id}"
                    or runtime.run_id != run.run_id
                    or str(runtime.mission_id) != run.binding["mission_id"]
                    or str(runtime.engagement_id) != run.binding["engagement_id"]
                    or str(runtime.instance_id) != value["instance_id"]
                    or runtime.work_owner != task.occupant_owner
                    or runtime.current_agent != task.assigned_agent
                    or value["runtime_digest"] != content_hash(runtime)
                    or value["completion_contract_hash"] != completion_contract_hash(task)
                    or value["input_binding_hash"] != run.identity["input_binding_hash"]
                    or runtime.component_binding is None
                    or runtime.component_binding.model_dump(mode="json")
                    != run.identity["components"]
                    or runtime.llm_request_policy != run.identity["llm_request_policy"]
                ):
                    raise ValueError("activation")
                activation = {
                    "run_id": run.run_id,
                    "instance_id": value["instance_id"],
                    "task_id": value["task_id"],
                    "owner": task.occupant_owner,
                    "ownership_version": max(1, task.ownership_version),
                    "launch_stage": stage,
                    "proof_version": 1,
                    "binding_hash": value["input_binding_hash"],
                }
                if activation["instance_id"] in run.activations:
                    raise ValueError("reused_instance")
                run.activations[activation["instance_id"]] = activation
                if task.ownership_version == 0:
                    # The source has no central generation field. Its unique
                    # original instance/launch proof supplies the first stored
                    # generation; preserve the source version/proof separately.
                    run.identity.setdefault("ownership_mapping", {})[str(task.task_id)] = {
                        "source_version": 0,
                        "central_version": 1,
                        "instance_id": activation["instance_id"],
                    }
                    for table, values in plan.writes:
                        if table == "state_tasks" and values["task_id"] == str(task.task_id):
                            active = MissionTask.model_validate_json(values["payload"])
                            values["payload"] = active.model_copy(
                                update={"ownership_version": 1},
                            ).model_dump_json()
                            for item in plan.mapping.values():
                                if item["target_table"] == table and item["target_key"] == (
                                    json.dumps({"run_id": run.run_id, "task_id": str(task.task_id)})
                                ):
                                    item["target_hash"] = digest(values)
                # Recovery/input proof kept as references/hashes, not Runtime copies.
                run.identity.setdefault("activation_proofs", {})[value["instance_id"]] = {
                    key: value.get(key)
                    for key in (
                        "goal_input_hash",
                        "task_input_hash",
                        "activation_generation",
                        "runtime_digest",
                    )
                }
                plan.mark(
                    "task",
                    "orchestration_records",
                    row,
                    reason="original_launch_proof",
                    target="state_activations",
                    values=activation,
                )
            elif kind in {"llm_request", "llm_request_policy", "llm_provider_cooldown"}:
                if kind == "llm_request":
                    RunRequestLedger.checked_record(identity, value)
                elif identity != run.run_id:
                    raise ValueError("request_identity")
                elif kind == "llm_provider_cooldown":
                    RunRequestLedger.checked_cooldown(value)
                plan.mark(
                    "task",
                    "orchestration_records",
                    row,
                    reason="original_accounting",
                    target="state_requests",
                    values={
                        "run_id": run.run_id,
                        "kind": kind,
                        "identity": identity,
                        "payload": json.dumps(value),
                    },
                )
            elif kind == "intent":
                intent = sanitize_intent_record(IntentRecord.model_validate(value))
                if (
                    str(intent.intent_id) != identity
                    or str(intent.intent.mission_id) != (run.binding["mission_id"])
                ):
                    raise ValueError("intent_identity")
                plan.mark(
                    "task",
                    "orchestration_records",
                    row,
                    reason="authoritative_intent",
                    target="state_intents",
                    values={
                        "run_id": run.run_id,
                        "intent_id": identity,
                        "status": intent.status.value,
                        "created_at": intent.intent.created_at.isoformat(),
                        "payload": intent.model_dump_json(),
                    },
                )
            elif kind == "outbox":
                event = EventEnvelope.model_validate(value["envelope"])
                if (
                    str(event.event_id) != identity
                    or str(event.run_id) != run.run_id
                    or str(event.engagement_id) != run.binding["engagement_id"]
                    or type(value["sequence"]) is not int
                    or value["sequence"] < 1
                    or type(value["delivered"]) is not bool
                    or not isinstance(value["lease_until"], (int, float))
                ):
                    raise ValueError("outbox_identity")
                plan.mark(
                    "task",
                    "orchestration_records",
                    row,
                    reason="original_delivery_state",
                    target="state_outbox",
                    values={
                        "run_id": run.run_id,
                        "event_id": identity,
                        "sequence": value["sequence"],
                        "payload": json.dumps(value),
                    },
                )
        except (ValueError, TypeError, KeyError, CoordinationError):
            plan.mark("task", "orchestration_records", row, reason="invalid_" + kind)
            run.reasons.add("original_" + kind + "_unverified")
    # History is a projection if the coordinator owns this same intent.
    for row in plan.snapshot.rows("intent", "operator_intents"):
        try:
            record = sanitize_intent_record(IntentRecord.model_validate_json(row["payload"]))
            authoritative = records.get(("intent", str(record.intent_id)))
            if authoritative is None:
                raise ValueError("intent_authority")
            current = sanitize_intent_record(authoritative)
            if check_intent_update(record, current) != current:
                raise ValueError("intent_history_ahead_of_authority")
            plan.mark("intent", "operator_intents", row, reason="intent_history_projection")
        except (ValueError, TypeError, KeyError):
            plan.mark("intent", "operator_intents", row, reason="intent_authority_unverified")
            plan.refuse("intent_authority_unverified")
    for run in plan.runs.values():
        owners = {a["owner"] for a in run.activations.values()}
        if any(t.occupant_owner and t.occupant_owner not in owners for t in run.tasks.values()):
            run.reasons.add("original_activation_missing")


def _prepare_graph(plan: _Import) -> None:
    rows = plan.snapshot.rows("graph", "graph_records")
    if not rows:
        return
    try:
        nodes, edges, _ = from_records([json.loads(row["record"]) for row in rows])
        models: dict[tuple[str, str], GraphNode | GraphEdge] = {
            ("nodes", str(n.id)): n for n in nodes
        }
        models.update({("edges", str(e.id)): e for e in edges})
        for row in rows:
            if row["collection"] == "graph_meta":
                plan.mark("graph", "graph_records", row, reason="snapshot_schema_1")
                continue
            record = models[(row["collection"], row["record_id"])]
            namespace, separator, _ = row["record_id"].partition("\x1f")
            legacy_edge = False
            if not separator and isinstance(record, GraphEdge):
                source_namespace, source_separator, _ = str(record.source_id).partition("\x1f")
                target_namespace, target_separator, _ = str(record.target_id).partition("\x1f")
                if source_separator and target_separator and source_namespace == target_namespace:
                    # Legacy wrappers encoded endpoints but left the edge key
                    # logical. Endpoints prove its namespace without guessing.
                    namespace, separator, legacy_edge = source_namespace, "\x1f", True
                    record = record.model_copy(update={"id": f"{namespace}\x1f{record.id}"})
            if not separator:
                plan.mark("graph", "graph_records", row, reason="graph_namespace_unproven")
                plan.refuse("graph_namespace_unproven")
                continue
            graph = GraphTransaction(
                plan.snapshot.connections["graph"], namespace=namespace, run_id="inspection"
            )
            if row["collection"] == "nodes":
                assert isinstance(record, GraphNode)
                graph.check_node(record)  # Physical schema validation, no merge or write.
                values = {
                    "namespace": namespace,
                    "node_id": str(record.id),
                    "node_type": record.type.value,
                    "payload": record.model_dump_json(),
                }
                table = "state_graph_nodes"
            else:
                assert isinstance(record, GraphEdge)
                graph.check_edge(record)
                if (row["source_id"], row["target_id"]) != (
                    str(record.source_id),
                    str(record.target_id),
                ):
                    raise ValueError("graph_edge_index")
                values = {
                    "namespace": namespace,
                    "edge_id": str(record.id),
                    "edge_type": record.type.value,
                    "source_id": str(record.source_id),
                    "target_id": str(record.target_id),
                    "payload": record.model_dump_json(),
                }
                table = "state_graph_edges"
            plan.mark(
                "graph",
                "graph_records",
                row,
                reason=(
                    "legacy_edge_namespace_from_endpoints"
                    if legacy_edge
                    else "original_graph_provenance"
                ),
                target=table,
                values=values,
            )
    except (ValueError, TypeError, KeyError, GraphMutationConflict):
        # Never retain a partially valid graph as if it were the complete one.
        plan.writes = [(t, r) for t, r in plan.writes if not t.startswith("state_graph_")]
        for row in rows:
            plan.mark("graph", "graph_records", row, reason="graph_schema_or_record_unreadable")
        plan.refuse("graph_schema_or_record_unreadable")
    for row in plan.snapshot.rows("graph", "graph_mutation_receipts"):
        try:
            receipt = GraphMutationReceipt.model_validate_json(row["receipt"])
            run = plan.runs[receipt.run_id]
            if (
                (receipt.namespace, receipt.run_id, receipt.mutation_id)
                != (
                    row["namespace"],
                    row["run_id"],
                    row["mutation_id"],
                )
                or run.engagement is None
                or receipt.namespace != run.binding["engagement_id"]
            ):
                raise ValueError("graph_receipt_identity")
            plan.mark(
                "graph",
                "graph_mutation_receipts",
                row,
                reason="original_mutation_receipt",
                target="state_graph_mutations",
                values=row,
            )
        except (ValueError, TypeError, KeyError):
            plan.mark("graph", "graph_mutation_receipts", row, reason="graph_receipt_unverified")
            plan.refuse("graph_receipt_unverified", row.get("run_id"))


async def _checkpoint_runtimes(plan: _Import) -> dict[tuple[str, str | None], RuntimeState]:
    """Constructor-free SDK reads on disposable copies; latest root only."""
    path = plan.snapshot.copies.get("checkpoint")
    found: dict[tuple[str, str | None], RuntimeState] = {}
    if path is None:
        return found
    rows = plan.snapshot.rows("checkpoint", "checkpoints")
    try:
        latest: dict[str, dict] = {}
        for row in rows:
            if row["checkpoint_ns"] == "":
                thread = row["thread_id"]
                if thread not in latest or row["checkpoint_id"] > latest[thread]["checkpoint_id"]:
                    latest[thread] = row
        async with open_identity_reader(backend="sqlite", path=str(path)) as saver:
            for thread, source in sorted(latest.items()):
                run_id, separator, instance = thread.partition(":instance:")
                run = plan.runs.get(run_id)
                if run is None or run.engagement is None:
                    continue
                if source["type"] != "msgpack":
                    run.reasons.add("checkpoint_format_or_identity_unverified")
                    continue
                config = {
                    "thread_id": thread,
                    "checkpoint_ns": "",
                    "checkpoint_id": source["checkpoint_id"],
                }
                try:
                    item = await saver.aget_tuple({"configurable": config})
                    if item is None or item.checkpoint.get("v") not in {1, 2}:
                        raise ValueError("checkpoint_version")
                except Exception:
                    run.reasons.add("checkpoint_format_or_identity_unverified")
                    continue
                values = item.checkpoint.get("channel_values", {})
                raw_runtime = values.get("runtime")
                if not isinstance(raw_runtime, dict) or set(raw_runtime) - set(
                    RuntimeState.model_fields
                ):
                    run.reasons.add("checkpoint_format_or_identity_unverified")
                    continue
                try:
                    runtime = RuntimeState.model_validate(raw_runtime)
                except ValueError:
                    run.reasons.add("checkpoint_format_or_identity_unverified")
                    continue
                expected_instance = instance if separator else None
                if (
                    str(runtime.run_id) != run_id
                    or runtime.instance_id != expected_instance
                    or str(runtime.engagement_id) != run.binding["engagement_id"]
                    or str(runtime.mission_id) != run.binding["mission_id"]
                    or runtime.component_binding is None
                    or runtime.component_binding.model_dump(mode="json")
                    != run.identity.get("components")
                    or runtime.llm_request_policy != run.identity.get("llm_request_policy")
                    or runtime.started_at is None
                ):
                    run.reasons.add("checkpoint_format_or_identity_unverified")
                    continue
                found[(run_id, expected_instance)] = runtime
                run.identity["checkpoints"].append(
                    {
                        "thread_id": thread,
                        "checkpoint_id": config["checkpoint_id"],
                        "runtime_digest": content_hash(runtime),
                        "started_at": runtime.started_at.isoformat(),
                        # Run accounting stays attached to the original instance.
                        "remaining_budget": runtime.remaining_budget,
                        "actions_executed": runtime.actions_executed,
                        "retry_tool_calls": runtime.retry_tool_calls,
                        "actions_by_agent": runtime.actions_by_agent,
                        "model_requests_used": runtime.model_requests_used,
                        "model_tokens_used": runtime.model_tokens_used,
                        "model_usage_unknown": runtime.model_usage_unknown,
                        "pending_action_id": runtime.pending_action_id,
                        "pending_approval_id": runtime.pending_approval_id,
                    }
                )
                if runtime.pending_approval_id:
                    # Pre-submission approvals still own their original
                    # proposal in the checkpoint; do not invent a receipt ID.
                    run.reasons.add("checkpoint_approval_requires_runtime_adapter")
    except Exception:  # SDK/schema failures become bounded refusals, never empty success.
        plan.refuse("checkpoint_format_or_identity_unverified")
    return found


async def _prepare_evidence_and_execution(
    plan: _Import,
    records: dict,
    evidence_store: EvidenceStore | None,
    control: ControlSnapshotInspection | None,
) -> None:
    for run in plan.runs.values():
        if run.engagement is None:
            continue
        try:

            async def inspect_launches(tx, run_id=run.run_id):
                await TaskActivation.inspect_migration_originals(tx, run_id)

            await inspect_sqlite_state(
                plan.snapshot.copies["task"], inspect_launches, include_paths=False
            )
        except Exception:
            run.reasons.add("original_activation_recovery_proof_unverified")
    checkpoints = await _checkpoint_runtimes(plan)
    work_rows = plan.snapshot.rows("work", "work_items")
    work = {}
    leases = {}
    for row in plan.snapshot.rows("work", "work_leases"):
        try:
            original_lease = TaskLease.model_validate(row)
            if original_lease.work_id != row["work_id"] or original_lease.owner != row["owner"]:
                raise ValueError("work_lease_index")
            leases[original_lease.work_id] = original_lease
        except (ValueError, TypeError, KeyError):
            plan.refuse("work_lease_unverified")
    for row in work_rows:
        try:
            item = WorkItem.model_validate_json(row["payload"])
            if (item.work_id, item.run_id, item.candidate_key, item.status.value) != (
                row["work_id"],
                row.get("run_id"),
                row["candidate_key"],
                row["status"],
            ) or ("instance_id" in row and row["instance_id"] != item.instance_id):
                raise ValueError("work_index")
            work[item.work_id] = (item, row)
            if (
                item.run_id in plan.runs
                and item.execution_origin_version != 1
                and (
                    item.attempt
                    or item.resource_execution_pending
                    or item.job_id
                    or item.status in {WorkStatus.RUNNING, WorkStatus.LEASED}
                )
            ):
                plan.refuse("legacy_execution_identity_unverified", str(item.run_id))
            plan.mark("work", "work_items", row, reason="candidate_queue_not_imported")
        except (ValueError, TypeError, KeyError):
            plan.mark("work", "work_items", row, reason="work_record_unreadable")
            plan.refuse("work_record_unreadable", row.get("run_id"))
    for run in plan.runs.values():
        if run.engagement is None:
            continue
        assert run.mission is not None
        if evidence_store is None or "evidence" not in plan.snapshot.references:
            run.reasons.add("original_evidence_store_unavailable")
            continue
        try:
            originals = await evidence_store.list_for_run(RunId(run.run_id))
            run.evidence = await check_evidence(
                evidence_store,
                run_id=run.run_id,
                engagement_id=run.binding["engagement_id"],
                evidence_ids=[str(item.evidence_id) for item in originals],
            )
            by_id = {str(item.record.evidence_id): item.record for item in run.evidence}
            refs: set[str] = set()
            for task in run.tasks.values():
                refs.update(map(str, task.evidence_ids))
                if task.completion_evidence_id:
                    refs.add(str(task.completion_evidence_id))
                if task.result:
                    refs.update(map(str, task.result.evidence_ids))
                    if task.result.completion_receipt_id:
                        refs.add(str(task.result.completion_receipt_id))
                    for question in task.result.questions:
                        refs.update(map(str, question.evidence_ids))
            if refs - by_id.keys():
                raise ValueError("task_evidence_missing")
            submissions = [
                r for r in originals if r.metadata.get("receipt_kind") == "execution_submission"
            ]
            seen: dict[str, str] = {}
            matched_work = set()
            converted_work = set()
            for submission in submissions:
                data = checked_receipt(submission)
                invocation = ToolInvocation.model_validate(data["invocation"])
                task = run.tasks[data["plan_step"]["mission_task_id"]]
                instance = str(invocation.execution_context.instance_id)
                activation = run.activations[instance]
                item, row = work[data["work_id"]]
                if (
                    item.run_id != run.run_id
                    or item.instance_id != instance
                    or item.execution_origin_version != 1
                    or item.mission_task_id != str(task.task_id)
                    or item.candidate_key != data["candidate_key"]
                    or item.model_tool_call_id != data["plan_step"]["model_tool_call_id"]
                    or item.task_purpose_hash != completion_contract_hash(task)
                    or activation["task_id"] != str(task.task_id)
                ):
                    raise ValueError("work_execution_identity")
                identity = str(invocation.invocation_id)
                if identity in seen:
                    if seen[identity] != content_hash(submission):
                        raise ValueError("conflicting_execution_original")
                    continue
                seen[identity] = content_hash(submission)
                matched_work.add(item.work_id)
                result = await preserved_result(
                    evidence_store,
                    run_id=RunId(run.run_id),
                    action_id=invocation.action_id,
                    instance_id=instance,
                    work_id=item.work_id,
                )
                state = ExecutionState.UNKNOWN
                result_id = None
                if result is not None:
                    original, result_id = result
                    if not execution_outcome_uncertain(original.metadata):
                        state = (
                            ExecutionState.SUCCEEDED if original.success else ExecutionState.FAILED
                        )
                lease = leases.get(item.work_id)
                if lease is not None and lease.owner != work_owner_for(
                    run.run_id, item.instance_id
                ):
                    raise ValueError("work_lease_owner")
                fingerprint = execution_fingerprint(
                    invocation.tool_id,
                    invocation.execution_context.approved_target,
                    invocation.arguments,
                )
                runtime = checkpoints.get((run.run_id, instance))
                if control is None or "audit" not in plan.snapshot.references:
                    raise ValueError("original_control_audit_missing")
                approval = await control.inspect_execution_approval(
                    ActionProposal.model_validate(data["proposal"]),
                )
                approval_id = None if approval is None else approval.approval_id
                if (
                    runtime is not None
                    and runtime.pending_approval_id is not None
                    and runtime.pending_approval_id != approval_id
                ):
                    raise ValueError("original_pending_approval_conflict")
                if approval_id:
                    if "approval" not in plan.snapshot.references:
                        raise ValueError("original_approval_store_missing")
                    if (
                        approval is None
                        or approval.action_id != invocation.action_id
                        or approval.execution_fingerprint != fingerprint
                    ):
                        raise ValueError("original_approval_mismatch")
                    run.identity["approvals"].append(
                        {
                            "approval_id": str(approval.approval_id),
                            "action_id": str(approval.action_id),
                            "status": approval.status.value,
                            "execution_fingerprint": approval.execution_fingerprint,
                            "original_hash": content_hash(approval),
                        }
                    )
                execution = TaskExecution.model_validate(
                    {
                        "engagement_id": run.binding["engagement_id"],
                        "run_id": run.run_id,
                        "task_id": task.task_id,
                        "instance_id": instance,
                        "ownership_version": activation["ownership_version"],
                        # Legacy journals do not record a goal revision. Revision
                        # one is unambiguous; later revisions stay explicitly unknown.
                        "goal_revision": 1 if run.mission.revision == 1 else None,
                        "action_id": invocation.action_id,
                        "invocation_id": invocation.invocation_id,
                        "model_tool_call_id": item.model_tool_call_id,
                        "tool_id": invocation.tool_id,
                        "input_digest": execution_input_digest(invocation),
                        "proposal_evidence_id": submission.evidence_id,
                        "approval_ref": approval_id,
                        "approval_fingerprint": fingerprint,
                        "state": state,
                        "job_ref": item.job_id,
                        "result_evidence_id": result_id,
                        "wait_reason": "approval"
                        if runtime is not None and runtime.pending_approval_id
                        else "job"
                        if item.job_id
                        else None,
                        "resource_keys": item.resource_keys,
                        "resource_execution_pending": item.resource_execution_pending,
                        "resource_lease_until": lease.expires_at.timestamp() if lease else 0,
                    }
                )
                check_execution_receipt(execution, submission, result=False, task=task)
                if state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
                    check_execution_receipt(execution, by_id[str(result_id)], result=True)
                run.executions.append(execution)
                # Correlated executions own these conflicts in the active index.
                converted_work.add(item.work_id)
                run.identity["held_resource_keys"] = sorted(
                    {
                        key
                        for original_work, _ in work.values()
                        if original_work.run_id == run.run_id
                        and original_work.work_id not in converted_work
                        and (original_work.resource_execution_pending or original_work.job_id)
                        for key in original_work.resource_keys
                    }
                )
                if execution.goal_revision is None:
                    run.reasons.add("original_execution_goal_revision_unverified")
                plan.mark(
                    "work",
                    "work_items",
                    row,
                    reason="original_execution_only",
                    target="state_executions",
                    values={
                        "run_id": run.run_id,
                        "invocation_id": identity,
                        "task_id": str(task.task_id),
                        "identity_hash": execution_identity_hash(execution),
                        "payload": execution.model_dump_json(),
                    },
                )
            if any(
                item.run_id == run.run_id
                and item.work_id not in matched_work
                and (
                    item.attempt
                    or item.job_id
                    or item.resource_execution_pending
                    or item.status in {WorkStatus.RUNNING, WorkStatus.LEASED}
                )
                for item, _ in work.values()
            ):
                run.reasons.add("work_without_original_submission")
            for instance, activation in run.activations.items():
                if activation["launch_stage"] != "reserved" and (
                    (run.run_id, instance) not in checkpoints
                ):
                    run.reasons.add("entered_instance_checkpoint_missing")
                if activation["launch_stage"] == "reserved" and (
                    any(e.instance_id == instance for e in run.executions)
                    or any(
                        k == "llm_request"
                        and isinstance(v, dict)
                        and v.get("owner") == activation["owner"]
                        for (k, _), v in records.items()
                    )
                ):
                    run.reasons.add("reserved_instance_has_activity")
        except Exception:
            # Retain any independently checked records; this Run stays fenced.
            run.reasons.add("execution_or_evidence_original_unverified")


def _prepare_network_and_history(plan: _Import, records: dict) -> dict | None:
    # Old planning locks never become prerequisites of the central engine.
    for row in plan.snapshot.rows("task", "orchestration_records"):
        value = records.get((row["kind"], row["identity"]))
        if row["kind"] in {"planning_reference", "planning_writer"}:
            pending = isinstance(value, dict) and (
                bool(value.get("writer"))
                if row["kind"] == "planning_reference"
                else value.get("state") != "confirmed"
            )
            run_id = next((r for r in plan.runs if row["identity"].startswith(r + ":")), None)
            if pending:
                plan.refuse("pending_planning_write_requires_original_confirmation", run_id)
            plan.mark(
                "task",
                "orchestration_records",
                row,
                reason=(
                    "pending_write_retained_resume_refused"
                    if pending
                    else "historical_planning_record"
                ),
            )
        elif row["kind"] == "task_activation_clock" and row["identity"] in plan.runs:
            plan.runs[row["identity"]].identity["started_at"] = value
        elif row["kind"] == "task_activation_policy" and row["identity"] in plan.runs:
            plan.runs[row["identity"]].identity["activation_policy"] = value
        elif row["kind"] == "outbox_sequence" and row["identity"] in plan.runs:
            if type(value) is int and value >= 0:
                plan.runs[row["identity"]].identity["outbox_sequence"] = value
            else:
                plan.refuse("original_outbox_sequence_unverified", row["identity"])
    for key, item in plan.mapping.items():
        if key[0] == "mission":
            item["reason"] = "unused_mission_history_retained"
        elif key[0] == "checkpoint":
            item["reason"] = "external_checkpoint_retained"
        elif key[0] == "task" and key[1].startswith("path_"):
            item["reason"] = "historical_path_not_a_prerequisite"
    if "network" not in plan.snapshot.connections:
        return None
    tables = ("network_pools", "network_windows", "network_seq_log")
    rows = {table: plan.snapshot.rows("network", table) for table in tables}
    try:
        if set(plan.snapshot.tables("network")) != set(tables):
            raise ValueError("network_schema")
        if plan.control is None:
            raise ValueError("network_snapshot_inspection_not_bound")
        plan.control.check_network(rows)
        for table, values in rows.items():
            for row in values:
                plan.mark(
                    "network",
                    table,
                    row,
                    reason="original_complete_network_accounting",
                    target=table,
                    values=row,
                )
        for row in rows["network_windows"]:
            run = plan.runs.get(row["run_id"])
            if (
                run is not None
                and row["instance_id"] in run.activations
                and (run.activations[row["instance_id"]]["launch_stage"] == "reserved")
            ):
                run.reasons.add("reserved_instance_has_network_activity")
        return rows
    except (ValueError, TypeError, KeyError):
        plan.refuse("network_accounting_or_schema_unverified")
        for table, values in rows.items():
            for row in values:
                plan.mark("network", table, row, reason="network_unverified_no_refund")
        return None


def _target_row(db: sqlite3.Connection, table: str, values: dict) -> dict | None:
    keys = _TARGET_KEYS[table]
    cursor = db.execute(
        f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys),
        tuple(values[key] for key in keys),
    )
    row = cursor.fetchone()
    return None if row is None else dict(zip((c[0] for c in cursor.description), row, strict=True))


def _write_import(plan: _Import, tx: CentralTransaction, network: dict | None) -> dict:
    db, request = tx.db, plan.request
    manifest = {
        **plan.snapshot.manifest,
        "adapter_version": 1,
        "source_runs": sorted(plan.runs),
        "ownership_runs": sorted(
            set(plan.runs)
            | {
                row["run_id"]
                for (source, table, _), row in plan.rows.items()
                if (source, table) == ("network", "network_windows")
            }
        ),
        "inputs_hashes": {r: content_hash(v) for r, v in request.inputs_by_run.items()},
    }
    source_hash = digest(manifest)
    previous = db.execute(
        "SELECT source_hash,report FROM state_migrations WHERE migration_id=?",
        (request.migration_id,),
    ).fetchone()
    if previous is not None:
        if previous[0] != source_hash:
            raise StateConflict("migration_identity_source_conflict")
        return json.loads(previous[1])
    # Another ID cannot reimport the same original cohort as a fresh Run/budget.
    for run_id in plan.runs:
        if db.execute("SELECT 1 FROM state_migrated_runs WHERE run_id=?", (run_id,)).fetchone():
            raise StateConflict("migration_run_already_imported")
        if db.execute("SELECT 1 FROM state_runs WHERE run_id=?", (run_id,)).fetchone():
            raise StateConflict("migration_target_run_already_exists")
    report = {
        "migration_id": request.migration_id,
        "adapter_version": 1,
        "source_hash": source_hash,
        "tables": plan.table_counts,
        "dispositions": dict(Counter(m["disposition"] for m in plan.mapping.values())),
        "reasons": dict(Counter(m["reason"] for m in plan.mapping.values())),
        "runs": {
            r: {"eligible_for_resume_validation": not run.reasons, "reasons": sorted(run.reasons)}
            for r, run in plan.runs.items()
        },
    }
    db.execute(
        "INSERT INTO state_migrations VALUES(?,?,?,?)",
        (request.migration_id, source_hash, json.dumps(manifest), json.dumps(report)),
    )
    # Parents precede children. Every write is an exact import; no graph merge,
    # intent application, accounting initialization or receipt fabrication.
    for table in _TARGET_KEYS:
        if table.startswith("network_"):
            continue
        for target, values in plan.writes:
            if target != table:
                continue
            previous_row = _target_row(db, table, values)
            if previous_row is not None:
                if digest(previous_row) != digest(values):
                    raise StateConflict("migration_target_record_conflict")
                continue
            db.execute(
                f"INSERT INTO {table} ({','.join(values)}) VALUES("
                + ",".join("?" for _ in values)
                + ")",
                tuple(values.values()),
            )
    if network is not None:
        if plan.control is None:
            raise ValueError("network_snapshot_inspection_not_bound")
        plan.control.import_network(db, network)
    for run in plan.runs.values():
        db.execute(
            "INSERT INTO state_migrated_runs VALUES(?,?,?,?)",
            (
                run.run_id,
                request.migration_id,
                json.dumps(run.identity),
                json.dumps(sorted(run.reasons)),
            ),
        )
        if run.engagement is None:
            continue
        for checked in run.evidence:
            tx_for_run = CentralTransaction(db, run.run_id, run.binding["engagement_id"])
            tx_for_run.add_evidence_refs([checked])
        for execution in run.executions:
            # Preserve conflicts on unknown/queued/job/uncleared resource work.
            if (
                execution.resource_execution_pending
                or execution.job_ref
                or execution.state
                not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
                or execution.resource_lease_until
            ):
                for key in execution.resource_keys:
                    db.execute(
                        "INSERT INTO state_execution_resources VALUES(?,?,?)",
                        (key, run.run_id, str(execution.invocation_id)),
                    )
    for (source, table, key), item in plan.mapping.items():
        db.execute(
            "INSERT INTO state_migration_records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                request.migration_id,
                source,
                table,
                key,
                item["source_hash"],
                item["disposition"],
                item["reason"],
                item["target_table"],
                item["target_key"],
                item["target_hash"],
            ),
        )
        if item["disposition"] == "active":
            target_record = _target_row(db, item["target_table"], json.loads(item["target_key"]))
            if target_record is None or digest(target_record) != item["target_hash"]:
                raise StateConflict("migration_target_reconciliation_failed")
    return report


async def migrate_legacy_storage(
    request: MigrationRequest,
    *,
    evidence_store: EvidenceStore | None = None,
    control: ControlSnapshotInspection | None = None,
) -> dict:
    """Storage-only entrypoint for plan2; explicitly stopped/offline sources required.

    ``references`` names the original evidence/artifact/audit/approval locations;
    stores supplied here must read those same originals. Omitted optional DBs
    are not created. All sharing Runs' original ownership is held during import.
    """
    if not request.offline:
        raise StateConflict("migration_requires_stopped_offline_sources")
    if not request.migration_id.strip() or "task" not in request.databases:
        raise ValueError("migration_requires_id_and_task_source")
    if set(request.databases) - {
        "task",
        "work",
        "graph",
        "intent",
        "mission",
        "checkpoint",
        "network",
    }:
        raise ValueError("migration_source_owner_unsupported")
    target = Path(request.target).resolve()
    sources = {Path(p).resolve() for p in request.databases.values()}
    if target in sources or any(
        target == Path(p).resolve() or Path(p).resolve() in target.parents
        for p in request.references.values()
    ):
        raise StateConflict("migration_target_overlaps_original")
    with OfflineSnapshot(dict(request.databases), dict(request.references)) as snapshot:
        for path in snapshot.paths.values():
            retirement = read_storage_retirement(path)
            if retirement is not None and (
                retirement["target"] != str(target)
                or retirement["migration_id"] != request.migration_id
            ):
                raise StateConflict("migration_source_already_retired_elsewhere")
        plan = _Import(snapshot, request, control)
        records = _prepare_runs(plan)
        ownership = sorted(
            set(plan.runs) | {row["run_id"] for row in snapshot.rows("network", "network_windows")}
        )
        with ExitStack() as owners:
            for run_id in ownership:
                owners.enter_context(
                    run_storage_ownership(snapshot.paths["task"], run_id, migrated_target=target)
                )
            snapshot.verify_originals()
            _prepare_tasks(plan)
            _prepare_records(plan, records)
            _prepare_graph(plan)
            await _prepare_evidence_and_execution(
                plan, records, evidence_store, control
            )
            network = _prepare_network_and_history(plan, records)
            snapshot.verify_originals()
            # setup creates only the explicitly selected target, never sources.
            store = CentralStateStore(target, run_id="migration", engagement_id="migration")
            await store.setup()
            report = await store.transaction(lambda tx: _write_import(plan, tx, network))
            snapshot.verify_originals()  # Outside the SQL lock; no fence if sources changed.
            retirement = {
                "version": 1,
                "target": str(target),
                "migration_id": request.migration_id,
                "source_hash": report["source_hash"],
            }
            for path in snapshot.paths.values():

                def retire_original(source=path):
                    retire_storage(source, retirement)

                await run_sqlite(retire_original)
            return report


async def validate_migrated_run(
    state: CentralStateStore,
    *,
    current_inputs: dict,
    current_policy: LLMRequestPolicy,
    component_check: Callable[[RuntimeComponentBinding], None],
    evidence_store: EvidenceStore,
    control: ControlSnapshotInspection | None = None,
) -> dict:
    """Read-only eligibility check. It does not acquire ownership or grant admission."""

    def read(tx):
        row = tx.db.execute(
            "SELECT migration_id,identity,reasons FROM state_migrated_runs WHERE run_id=?",
            (state.run_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("migration_run_missing")
        manifest = tx.db.execute(
            "SELECT manifest FROM state_migrations WHERE migration_id=?", (row[0],)
        ).fetchone()[0]
        refs = tx.db.execute(
            "SELECT evidence_id,content_hash FROM state_evidence_refs WHERE run_id=?",
            (state.run_id,),
        ).fetchall()
        executions = [
            TaskExecution.model_validate_json(r[0])
            for r in tx.db.execute(
                "SELECT payload FROM state_executions WHERE run_id=?",
                (state.run_id,),
            )
        ]
        return (
            row[0],
            json.loads(row[1]),
            json.loads(row[2]),
            json.loads(manifest),
            dict(refs),
            executions,
        )

    migration_id, identity, reasons, manifest, refs, executions = await state.transaction(
        read,
        write=False,
    )
    if reasons:
        raise StateConflict("migration_run_resume_refused:" + ",".join(reasons))
    if content_hash(current_inputs) != identity.get(
        "input_binding_hash"
    ) or current_policy.model_dump(mode="json") != identity.get("llm_request_policy"):
        raise StateConflict("migration_current_binding_changed")
    component_check(RuntimeComponentBinding.model_validate(identity["components"]))
    paths = {name: Path(p) for name, p in manifest["databases"].items()}
    expected_retirement = {
        "version": 1,
        "target": str(state.path.resolve()),
        "migration_id": migration_id,
        "source_hash": digest(manifest),
    }
    if any(read_storage_retirement(path) != expected_retirement for path in paths.values()):
        raise StateConflict("migration_destination_fence_missing_or_mismatched")
    # Old shared graph/pools/checkpoints stay frozen; never run two writable ledgers.
    current = await run_sqlite(lambda: source_manifest(paths, {}))
    if any(
        current["files"].get(path) != value
        for path, value in manifest["files"].items()
        if path in current["files"]
    ):
        raise StateConflict("migration_original_source_changed")
    if any(not Path(p).exists() for p in manifest["references"].values()):
        raise StateConflict("migration_original_reference_missing")
    checked = await check_evidence(
        evidence_store,
        run_id=state.run_id,
        engagement_id=state.engagement_id,
        evidence_ids=list(refs),
    )
    if any(refs[str(item.record.evidence_id)] != item.content_hash for item in checked):
        raise StateConflict("migration_original_evidence_changed")
    by_id = {str(item.record.evidence_id): item.record for item in checked}
    for execution in executions:
        if control is None:
            raise StateConflict("migration_original_control_audit_missing")
        data = checked_receipt(by_id[str(execution.proposal_evidence_id)])
        approval = await control.inspect_execution_approval(
            ActionProposal.model_validate(data["proposal"]),
        )
        if (None if approval is None else str(approval.approval_id)) != execution.approval_ref:
            raise StateConflict("migration_original_control_approval_changed")
    for approval in identity["approvals"]:
        if control is None:
            raise StateConflict("migration_original_approval_store_missing")
        original = await control.get_approval(approval["approval_id"])
        if (
            original is None
            or str(original.action_id) != approval["action_id"]
            or original.execution_fingerprint != approval["execution_fingerprint"]
        ):
            raise StateConflict("migration_original_approval_changed")
    return {"identity": identity, "manifest": manifest}


@asynccontextmanager
async def migrated_run_guard(state: CentralStateStore, **validation) -> AsyncIterator[dict]:
    """Hold original cohort ownership for the ENTIRE new execution scope.

    The target path cannot create independent ownership of the same original
    Run or split a shared network budget. The caller keeps old sources offline;
    plan2 must hold this scope before consuming any migrated state.
    """
    result = await validate_migrated_run(state, **validation)
    manifest = result["manifest"]
    with ExitStack() as owners:
        for run_id in manifest["ownership_runs"]:
            owners.enter_context(
                run_storage_ownership(
                    manifest["databases"]["task"], run_id, migrated_target=state.path
                )
            )
        # Close the check/acquire gap before enabling central admission.
        result = await validate_migrated_run(state, **validation)
        permit = MigrationPermit()
        token = state.migration_permit.set(permit)
        try:
            yield result["identity"]
        finally:
            permit.active = False
            state.migration_permit.reset(token)
