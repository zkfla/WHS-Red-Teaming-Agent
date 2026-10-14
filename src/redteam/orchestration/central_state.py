"""Default central SQLite state, without Path, Work DAG or a new coordinator.

Transactions are synchronous callbacks on a worker-owned connection. Participants
use ``tx.db`` and never commit it; the outer store alone commits or rolls back.
External evidence inspection, providers and event delivery happen outside here.
JSON holds each domain record once; SQL columns/indexes are lookup projections.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

from redteam.attack_graph.stores.central import GRAPH_SCHEMA, GraphTransaction
from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.contracts.graph import Hypothesis, HypothesisUpdate
from redteam.core.contracts.mission import (
    InvestigationResult,
    MissionPlan,
    MissionTask,
    redact_secret_material,
)
from redteam.core.contracts.policy import Approval
from redteam.core.contracts.tool import ToolInvocation, ToolResult, execution_outcome_uncertain
from redteam.core.convert import execution_input_digest
from redteam.core.enums import ApprovalStatus, MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import EngagementId, EvidenceId, MissionId, OperatorIntentId, RunId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.tool_binding import execution_fingerprint
from redteam.orchestration.coordination import (
    CoordinationError,
    PlanningInputsChanged,
    StateConflict,
    content_hash,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.execution_journal import checked_receipt, preserved_result
from redteam.orchestration.llm_requests import (
    RequestAdmissionError,
    RequestChange,
    RunRequestLedger,
)
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from redteam.orchestration.operator_intents.store import check_intent_update, sanitize_intent_record
from redteam.orchestration.outbox import OutboxChange, deliver_pending
from redteam.orchestration.resource_conflicts import INVOCATION_RESOURCE_PREFIX
from redteam.sqlite_support import connect, run_sqlite, run_transaction

T = TypeVar("T")
ActivityCheck = Callable[[sqlite3.Connection, str, str, Sequence[str], bool], bool]
SCHEMA_VERSION = 3
_CLOSED = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
_RESOLVED = {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
_BLOCKING_INTENTS = {
    OperatorIntentType.PAUSE.value, OperatorIntentType.STOP.value,
    OperatorIntentType.EXCLUDE.value,
}

# Version zero is an empty foundation. Later stages add their own upgrade here,
# not PRAGMA user_version (other owners, including network, share this file).
_SCHEMA_1 = (
    "CREATE TABLE state_missions (mission_id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, "
    "payload TEXT NOT NULL)",
    "CREATE INDEX state_mission_engagement ON state_missions(engagement_id)",
    "CREATE TABLE state_runs (run_id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL, "
    "mission_id TEXT NOT NULL UNIQUE REFERENCES state_missions(mission_id), "
    "scope_json TEXT NOT NULL, binding_hash TEXT NOT NULL)",
    "CREATE TABLE state_tasks (run_id TEXT NOT NULL REFERENCES state_runs(run_id), "
    "task_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,task_id))",
    "CREATE INDEX state_task_assignment ON state_tasks(run_id, "
    "json_extract(payload,'$.status'),json_extract(payload,'$.occupant_owner'))",
    "CREATE TABLE state_registrations (run_id TEXT NOT NULL, request_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL, request_hash TEXT NOT NULL, PRIMARY KEY(run_id,request_id), "
    "FOREIGN KEY(run_id,task_id) REFERENCES state_tasks(run_id,task_id))",
    "CREATE TABLE state_executions (run_id TEXT NOT NULL, invocation_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL, identity_hash TEXT NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(run_id,invocation_id), "
    "FOREIGN KEY(run_id,task_id) REFERENCES state_tasks(run_id,task_id))",
    "CREATE INDEX state_execution_task ON state_executions(run_id,task_id)",
    # An indexed projection of opaque keys, not another resource/claim lifecycle.
    "CREATE TABLE state_execution_resources (resource_key TEXT NOT NULL, run_id TEXT NOT NULL, "
    "invocation_id TEXT NOT NULL, PRIMARY KEY(resource_key,run_id,invocation_id), "
    "FOREIGN KEY(run_id,invocation_id) REFERENCES state_executions(run_id,invocation_id))",
    "CREATE TABLE state_evidence_refs (run_id TEXT NOT NULL REFERENCES state_runs(run_id), "
    "evidence_id TEXT NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY(run_id,evidence_id))",
    "CREATE TABLE state_intents (run_id TEXT NOT NULL REFERENCES state_runs(run_id), "
    "intent_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY(run_id,intent_id))",
    "CREATE INDEX state_pending_intents ON state_intents(run_id,status,created_at,intent_id)",
    "CREATE TABLE state_requests (run_id TEXT NOT NULL REFERENCES state_runs(run_id), "
    "kind TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(run_id,kind,identity))",
    "CREATE TABLE state_activations (run_id TEXT NOT NULL, instance_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL, owner TEXT NOT NULL, ownership_version INTEGER NOT NULL, "
    "launch_stage TEXT NOT NULL CHECK(launch_stage IN ('reserved','entered','returned')), "
    "proof_version INTEGER NOT NULL, binding_hash TEXT NOT NULL, "
    "PRIMARY KEY(run_id,instance_id), "
    "FOREIGN KEY(run_id,task_id) REFERENCES state_tasks(run_id,task_id))",
    "CREATE INDEX state_activation_task ON state_activations(run_id,task_id,ownership_version)",
    "CREATE TABLE state_outbox (run_id TEXT NOT NULL REFERENCES state_runs(run_id), "
    "event_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(run_id,event_id), UNIQUE(run_id,sequence))",
    "CREATE INDEX state_outbox_delivery ON state_outbox "
    "(run_id,json_extract(payload,'$.delivered'),sequence)",
)


_SCHEMA_3 = (
    "CREATE TABLE state_migrations (migration_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, "
    "manifest TEXT NOT NULL, report TEXT NOT NULL)",
    # Source bodies stay in retained originals; only mapping/hash/reason goes here.
    "CREATE TABLE state_migration_records (migration_id TEXT NOT NULL "
    "REFERENCES state_migrations(migration_id), source TEXT NOT NULL, source_table TEXT NOT NULL, "
    "source_key TEXT NOT NULL, source_hash TEXT NOT NULL, disposition TEXT NOT NULL, "
    "reason TEXT NOT NULL, target_table TEXT, target_key TEXT, target_hash TEXT, "
    "PRIMARY KEY(migration_id,source,source_table,source_key))",
    "CREATE TABLE state_migrated_runs (run_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL "
    "REFERENCES state_migrations(migration_id), identity TEXT NOT NULL, reasons TEXT NOT NULL)",
)


@dataclass
class MigrationPermit:
    """An execution-scope proof, revoked when original ownership is released."""

    active: bool = True


@dataclass(frozen=True)
class CheckedEvidence:
    """Ephemeral inspection result; only identity/hash is persisted, never its body."""

    record: EvidenceRecord
    content_hash: str


async def check_evidence(
    store: EvidenceStore, *, run_id: str, engagement_id: str, evidence_ids: Sequence[str],
) -> tuple[CheckedEvidence, ...]:
    """Inspect originals before taking the SQLite lock; unrelated IDs are not fenced."""
    verifier = getattr(store, "verify_run_integrity", None)
    if not callable(verifier):
        raise CoordinationError("evidence_integrity_inspection_unavailable")
    bad_ids = set(await verifier(RunId(run_id)))
    checked = []
    for identity in sorted(set(evidence_ids)):
        if identity in bad_ids:
            raise StateConflict("evidence_file_index_disagreement")
        record = await store.get(EvidenceId(identity))
        if record is None:
            raise StateConflict("original_evidence_missing")
        if str(record.run_id) != run_id or str(record.engagement_id) != engagement_id:
            raise StateConflict("evidence_scope_mismatch")
        if record.metadata.get("receipt_kind") in {"execution_result", "execution_resolution"}:
            # Reuse the existing original/resolution chain validation, including
            # lost-ack originals. This file I/O stays outside the SQLite lock.
            payload = checked_receipt(record)
            if record.action_id is None:
                raise StateConflict("execution_original_action_missing")
            found = await preserved_result(
                store, run_id=record.run_id, action_id=record.action_id,
                instance_id=payload.get("instance_id"), work_id=payload.get("work_id"),
            )
            if found is None or found[1] != record.evidence_id:
                raise StateConflict("execution_result_original_not_proven")
        checked.append(CheckedEvidence(record, content_hash(record)))
    return tuple(checked)


def execution_identity_hash(record: TaskExecution) -> str:
    excluded = {
        "state", "wait_reason", "job_ref", "result_evidence_id",
        "resource_execution_pending", "resource_lease_until",
    }
    if record.target_node_id is None:
        excluded.add("target_node_id")  # Preserve historical identity hashes.
    if not record.target_identity_verified:
        excluded.add("target_identity_verified")
    return content_hash(record.model_dump(mode="json", exclude=excluded))


def execution_occupies_capacity(record: TaskExecution) -> bool:
    """Original reservations and external activity survive actor/lease return."""
    return (
        record.state in {ExecutionState.RUNNING, ExecutionState.UNKNOWN}
        or record.job_ref is not None
        or record.resource_execution_pending
        or (record.state is ExecutionState.PENDING and record.wait_reason is None)
    )


def _finish_record_callback(coroutine) -> Any:
    """Legacy accounting/delivery callbacks contain only synchronous record operations."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        coroutine.close()
        raise TypeError("record transaction callback must not await external I/O")


def check_execution_receipt(
    execution: TaskExecution, record: EvidenceRecord, *, result: bool,
    task: MissionTask | None = None,
) -> None:
    try:
        payload = checked_receipt(record)
        invocation = ToolInvocation.model_validate(payload["invocation"])
        step = payload["plan_step"]
        if (record.action_id != execution.action_id
                or record.invocation_id != execution.invocation_id
                or invocation.tool_id != execution.tool_id
                or invocation.execution_context.instance_id != execution.instance_id
                or execution_input_digest(invocation) != execution.input_digest
                or step.get("mission_task_id") != str(execution.task_id)
                or step.get("model_tool_call_id") != execution.model_tool_call_id
                or invocation.arguments != payload["proposal"]["arguments"]):
            raise ValueError("identity")
        if execution.approval_fingerprint is not None and (
            execution_fingerprint(invocation.tool_id, invocation.execution_context.approved_target,
                                  invocation.arguments) != execution.approval_fingerprint
        ):
            raise ValueError("approval identity")
        if not result:
            if (payload["receipt_kind"] != "execution_submission" or task is None
                    or step.get("task_purpose_hash") != completion_contract_hash(task)):
                raise ValueError("submission")
            if execution.target_identity_verified or execution.target_node_id is not None:
                from redteam.attack_graph.identity import node_id_for_target

                target = invocation.execution_context.approved_target
                expected = str(node_id_for_target(target)) if target is not None else None
                if expected != execution.target_node_id:
                    raise ValueError("target identity")
            return
        if payload["receipt_kind"] not in {"execution_result", "execution_resolution"}:
            raise ValueError("result")
        original = ToolResult.model_validate(payload["original_result"])
        if (original.action_id != execution.action_id
                or original.invocation_id != execution.invocation_id
                or original.tool_id != execution.tool_id
                or (execution_outcome_uncertain(original.metadata)
                    and execution.state is not ExecutionState.UNKNOWN)
                or (execution.state is not ExecutionState.UNKNOWN
                    and ((execution.state is ExecutionState.SUCCEEDED) != original.success))):
            raise ValueError("result outcome")
    except ValueError:
        raise StateConflict("execution_original_receipt_mismatch_or_uncertain") from None


class CentralTransaction:
    """One Run's row operations, on the outer transaction's connection."""

    def __init__(
        self, db: sqlite3.Connection, run_id: str, engagement_id: str,
        activity_check: ActivityCheck | None = None,
        migration_permit: MigrationPermit | None = None,
    ) -> None:
        self.db, self.run_id, self.engagement_id = db, run_id, engagement_id
        self.activity_check = activity_check
        self.migration_permit = migration_permit

    def binding(self) -> tuple[str, str]:
        row = self.db.execute(
            "SELECT mission_id,binding_hash FROM state_runs WHERE run_id=? AND engagement_id=?",
            (self.run_id, self.engagement_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("run_binding_missing_or_mismatched")
        return row[0], row[1]

    def save_planning_diagnostics(
        self, diagnostic: DecisionDiagnostic | None,
        history: Sequence[DecisionDiagnostic], *, termination_reason: str,
    ) -> None:
        """Bind safe terminal diagnostics to the current mission revision."""
        mission = self.get_mission(self.binding()[0])
        if mission is None:
            raise StateConflict("central_original_mission_missing")
        if (self.active_activation_count() or not mission.termination_reason
                or mission.termination_reason != termination_reason):
            # A partial settlement must not publish a terminal diagnostic or
            # fail an existing stop path whose mission has no terminal reason.
            return
        payload = {
            "mission_id": str(mission.mission_id), "mission_revision": mission.revision,
            "mission_status": mission.status.value, "termination_reason": termination_reason,
            "diagnostic": diagnostic.model_dump(mode="json") if diagnostic else None,
            "history": [item.model_dump(mode="json") for item in history],
        }
        self.db.execute(
            "INSERT INTO state_requests VALUES(?,'planning_diagnostics',?,?) "
            "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
            (self.run_id, self.run_id, json.dumps(payload)),
        )

    def planning_diagnostics(self) -> tuple[DecisionDiagnostic | None, list[DecisionDiagnostic]]:
        """Read matching diagnostics only; resume/live state cannot reuse an old failure."""
        mission = self.get_mission(self.binding()[0])
        if mission is None:
            raise StateConflict("central_original_mission_missing")
        if self.active_activation_count() or not mission.termination_reason:
            return None, []
        row = self.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? "
            "AND kind='planning_diagnostics' AND identity=?", (self.run_id, self.run_id),
        ).fetchone()
        if row is None:
            return None, []
        payload = json.loads(row[0])
        expected = (str(mission.mission_id), mission.revision, mission.status.value,
                    mission.termination_reason)
        actual = tuple(payload.get(key) for key in (
            "mission_id", "mission_revision", "mission_status", "termination_reason",
        ))
        if actual != expected:
            return None, []
        raw = payload.get("diagnostic")
        return (
            DecisionDiagnostic.model_validate(raw) if raw is not None else None,
            [DecisionDiagnostic.model_validate(item) for item in payload.get("history", [])],
        )

    @property
    def graph(self) -> GraphTransaction:
        """Graph participant on this connection; the outer transaction commits."""
        self.binding()
        return GraphTransaction(self.db, namespace=self.engagement_id, run_id=self.run_id)

    def bind(self, mission_id: str, scope: EngagementScope, binding_hash: str) -> None:
        if len(binding_hash) != 64 or any(c not in "0123456789abcdef" for c in binding_hash):
            raise CoordinationError("run_binding_hash_invalid")
        mission = self.get_mission(mission_id)
        if mission is None:
            raise CoordinationError("run_binding_missing_or_mismatched")
        desired = (self.engagement_id, mission_id, scope.model_dump_json(), binding_hash)
        saved = self.db.execute(
            "SELECT engagement_id,mission_id,scope_json,binding_hash FROM state_runs "
            "WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        if saved is not None:
            if saved != desired:
                raise StateConflict("run_binding_changed_new_run_required")
            return
        self.db.execute(
            "INSERT INTO state_runs VALUES(?,?,?,?,?)", (self.run_id, *desired),
        )

    def get_mission(self, mission_id: str) -> MissionPlan | None:
        row = self.db.execute(
            "SELECT m.payload FROM state_missions m LEFT JOIN state_runs r "
            "ON r.mission_id=m.mission_id WHERE m.mission_id=? AND m.engagement_id=? "
            "AND (r.run_id IS NULL OR r.run_id=?)",
            (mission_id, self.engagement_id, self.run_id),
        ).fetchone()
        return MissionPlan.model_validate_json(row[0]).redacted_copy() if row else None

    def create_mission(self, mission: MissionPlan) -> None:
        mission = mission.redacted_copy()
        if str(mission.engagement_id) != self.engagement_id:
            raise CoordinationError("mission_engagement_mismatch")
        self.db.execute("INSERT INTO state_missions VALUES(?,?,?)", (
            str(mission.mission_id), self.engagement_id, mission.model_dump_json(),
        ))

    def update_mission(self, mission: MissionPlan, *, expected_revision: int) -> None:
        mission = mission.redacted_copy()
        bound, _ = self.binding()
        previous = self.get_mission(str(mission.mission_id))
        if (bound != str(mission.mission_id) or previous is None
                or str(mission.engagement_id) != self.engagement_id
                or previous.revision != expected_revision
                or mission.revision != expected_revision + 1):
            raise StateConflict("mission_revision_or_scope_mismatch")
        if previous.status in _CLOSED:
            raise StateConflict("mission_already_closed")
        self.db.execute("UPDATE state_missions SET payload=? WHERE mission_id=?", (
            mission.model_dump_json(), str(mission.mission_id),
        ))

    def admit(self, *, settlement: bool = False) -> MissionPlan:
        migration = self.db.execute(
            "SELECT reasons FROM state_migrated_runs WHERE run_id=?", (self.run_id,),
        ).fetchone()
        if migration is not None:
            if json.loads(migration[0]):
                raise CoordinationError("migration_run_resume_refused")
            if self.migration_permit is None or not self.migration_permit.active:
                raise CoordinationError("migration_resume_guard_required")
        mission_id, _ = self.binding()
        mission = self.get_mission(mission_id)
        if mission is None or mission.status is not MissionStatus.ACTIVE:
            raise CoordinationError("mission_not_active")
        pending = self.db.execute(
            "SELECT 1 FROM state_intents WHERE run_id=? AND status='received' "
            "AND json_extract(payload,'$.intent.intent_type') IN (?,?,?) LIMIT 1",
            (self.run_id, *_BLOCKING_INTENTS),
        ).fetchone()
        if pending:
            raise StateConflict("pending_operator_mutation")
        if not settlement and self.settlement_closed():
            raise StateConflict("goal_settlement_admission_closed")
        return mission

    def settlement_closed(self) -> bool:
        row = self.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? AND kind='goal_settlement' "
            "AND identity=?", (self.run_id, self.run_id),
        ).fetchone()
        return bool(row and json.loads(row[0]).get("admission_closed"))

    def set_settlement_gate(self, closed: bool) -> None:
        self.binding()
        self.db.execute(
            "INSERT INTO state_requests VALUES(?,'goal_settlement',?,?) "
            "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
            (self.run_id, self.run_id, json.dumps({"admission_closed": closed})),
        )

    def get_task(self, task_id: str) -> MissionTask | None:
        self.binding()
        row = self.db.execute(
            "SELECT payload FROM state_tasks WHERE run_id=? AND task_id=?", (self.run_id, task_id),
        ).fetchone()
        return MissionTask.model_validate_json(row[0]).redacted_copy() if row else None

    def _write_task(self, task: MissionTask) -> MissionTask:
        task = task.redacted_copy()
        # Do not persist the inactive legacy investigation scheduler fields.
        payload = task.model_dump_json(
            exclude={"dependency_task_ids", "parent_task_id", "priority"},
        )
        self.db.execute("UPDATE state_tasks SET payload=? WHERE run_id=? AND task_id=?", (
            payload, self.run_id, str(task.task_id),
        ))
        return task

    def list_tasks(self) -> list[MissionTask]:
        self.binding()
        return [MissionTask.model_validate_json(row[0]).redacted_copy() for row in self.db.execute(
            "SELECT payload FROM state_tasks WHERE run_id=? ORDER BY task_id", (self.run_id,),
        )]

    def add_evidence_refs(self, checked: Sequence[CheckedEvidence]) -> None:
        self.binding()
        for item in checked:
            record = item.record
            if (str(record.run_id) != self.run_id or str(record.engagement_id) != self.engagement_id
                    or item.content_hash != content_hash(record)):
                raise StateConflict("evidence_scope_or_inspection_changed")
            row = self.db.execute(
                "SELECT content_hash FROM state_evidence_refs WHERE run_id=? AND evidence_id=?",
                (self.run_id, str(record.evidence_id)),
            ).fetchone()
            if row is not None and row[0] != item.content_hash:
                raise StateConflict("immutable_evidence_id_conflict")
            self.db.execute("INSERT OR IGNORE INTO state_evidence_refs VALUES(?,?,?)", (
                self.run_id, str(record.evidence_id), item.content_hash,
            ))

    def _require_refs(self, identities: Sequence[str]) -> None:
        for identity in set(identities):
            if self.db.execute(
                "SELECT 1 FROM state_evidence_refs WHERE run_id=? AND evidence_id=?",
                (self.run_id, identity),
            ).fetchone() is None:
                raise StateConflict("evidence_reference_not_inspected")

    def register_task(
        self, request_id: str, task: MissionTask, *, checked: Sequence[CheckedEvidence] = (),
    ) -> MissionTask:
        task = task.redacted_copy()
        mission_id, _ = self.binding()
        if (not request_id.strip() or str(task.mission_id) != mission_id
                or not str(task.task_id).strip()):
            raise CoordinationError("task_registration_scope_invalid")
        if (task.dependency_task_ids or task.parent_task_id or task.priority != 0
                or task.occupant_owner is not None or task.ownership_version != 0
                or task.result is not None
                or task.status not in {TaskStatus.PENDING, TaskStatus.READY}):
            raise CoordinationError("registration_requires_unowned_investigation_without_dag")
        request_hash = content_hash(task)
        previous = self.db.execute(
            "SELECT task_id,request_hash FROM state_registrations WHERE run_id=? AND request_id=?",
            (self.run_id, request_id),
        ).fetchone()
        if previous:
            if previous[1] != request_hash:
                raise StateConflict("registration_request_identity_conflict")
            saved = self.get_task(previous[0])
            assert saved is not None
            return saved
        self.admit()
        self.add_evidence_refs(checked)
        self._require_refs(task.evidence_ids)
        if task.source_task_id is not None:
            source = self.get_task(str(task.source_task_id))
            if (source is None or source.result is None
                    or source.result.result_id != task.source_result_id
                    or (task.source_question_id is not None and task.source_question_id not in {
                        q.question_id for q in source.result.questions
                    })):
                raise StateConflict("follow_up_source_missing_or_mismatched")
        if task.investigation_contract_version == 2:
            from redteam.attack_graph.identity import hypothesis_node_id
            from redteam.attack_graph.predicates import facts_of

            for identity, revision in task.hypothesis_revisions.items():
                node = self.graph.get_local_node(hypothesis_node_id(identity))
                candidate = Hypothesis.model_validate(facts_of(node.properties)) if node else None
                if candidate is None or candidate.contract_version != 2 or (
                    candidate.revision != revision
                ):
                    raise StateConflict("probe_candidate_revision_changed")
                if set(map(str, task.related_attack_graph_node_ids)) - set(
                    candidate.target_node_ids
                ):
                    raise StateConflict("probe_target_outside_candidate")
                if set(task.principal_refs) - set(candidate.principal_refs):
                    raise StateConflict("probe_principal_outside_candidate")
        payload = task.model_dump_json(
            exclude={"dependency_task_ids", "parent_task_id", "priority"},
        )
        self.db.execute("INSERT INTO state_tasks VALUES(?,?,?)", (
            self.run_id, str(task.task_id), payload,
        ))
        self.db.execute("INSERT INTO state_registrations VALUES(?,?,?,?)", (
            self.run_id, request_id, str(task.task_id), request_hash,
        ))
        return task

    def _owned(self, task_id: str, owner: str, version: int) -> MissionTask:
        task = self.get_task(task_id)
        if task is None or task.occupant_owner != owner or task.ownership_version != version:
            raise StateConflict("stale_task_ownership")
        return task

    def probe_usage(self, task_id: str) -> dict:
        """Read existing execution and model reservation rows, never reset per actor."""
        task = self.get_task(task_id)
        if task is None:
            raise StateConflict("probe_task_missing")
        actions = self.db.execute(
            "SELECT count(*) FROM state_executions WHERE run_id=? AND task_id=?",
            (self.run_id, task_id),
        ).fetchone()[0]
        requests = self.db.execute(
            "SELECT count(*) FROM state_requests WHERE run_id=? AND kind='llm_request' "
            "AND json_extract(payload,'$.task_id')=? "
            "AND json_extract(payload,'$.state')!='released'", (self.run_id, task_id),
        ).fetchone()[0]
        return {"actions": actions, "model_requests": requests,
                "limits": task.probe_limits.model_dump() if task.probe_limits else None}

    def register_batch(
        self, request_id: str, tasks: Sequence[MissionTask], *,
        checked: Sequence[CheckedEvidence] = (),
    ) -> list[MissionTask]:
        """One registration request, even if every proposed child key changes."""
        self.binding()
        if not request_id.strip() or not tasks:
            raise StateConflict("registration_batch_empty_or_missing_identity")
        request = "batch:" + content_hash(request_id)
        request_hash = content_hash([
            task.redacted_copy().model_dump(mode="json") for task in tasks
        ])
        previous = self.db.execute(
            "SELECT request_hash FROM state_registrations WHERE run_id=? AND request_id=?",
            (self.run_id, request),
        ).fetchone()
        if previous is not None:
            if previous[0] != request_hash:
                raise StateConflict("registration_request_identity_conflict")
            originals = [self.get_task(str(task.task_id)) for task in tasks]
            if any(item is None for item in originals):
                raise StateConflict("registration_original_task_missing")
            return [item for item in originals if item is not None]
        saved = [self.register_task(
            "item:" + content_hash([request_id, str(task.task_id)]), task, checked=checked,
        ) for task in tasks]
        self.db.execute("INSERT INTO state_registrations VALUES(?,?,?,?)", (
            self.run_id, request, str(saved[0].task_id), request_hash,
        ))
        return saved

    def probe_registration(self, identity: str, request_hash: str) -> dict | None:
        """Exact receipt identity, stored on the existing Run request ledger."""
        self.binding()
        row = self.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? "
            "AND kind='hypothesis_probe_registration' AND identity=?", (self.run_id, identity),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if payload["request_hash"] != request_hash:
            raise StateConflict("registration_request_identity_conflict")
        if any(self.get_task(identity) is None for identity in payload["task_ids"]):
            raise StateConflict("registration_original_task_missing")
        return payload

    def register_hypothesis_probes(
        self, request_id: str, request_hash: str, *, updates: Sequence[HypothesisUpdate],
        tasks: Sequence[MissionTask], checked: Sequence[CheckedEvidence],
        expected_revision: int, expected_binding_hash: str, graph_read: Mapping[str, str],
        notifications: Sequence[EventEnvelope], validity_read: Any = None,
        task_read: Mapping[str, str] | None = None,
        planner_judgment: dict | None = None,
    ) -> dict:
        """Candidate changes, native Tasks, checked refs, receipt and outbox commit once."""
        from redteam.attack_graph.hypotheses import hypothesis_purpose
        from redteam.attack_graph.identity import hypothesis_node_id
        from redteam.core.ids import GraphNodeId

        previous = self.probe_registration(request_id, request_hash)
        if previous is not None:
            return previous
        self.check_planning_inputs(expected_revision, expected_binding_hash, graph_read, task_read)
        if planner_judgment is not None:
            self.check_planner_inventory(planner_judgment)
        if validity_read is not None:
            validity_read.check(self)
        self.add_evidence_refs(checked)
        inspected = {str(item.record.evidence_id): item.record for item in checked}
        revisions = {}
        for update in updates:
            candidate = update.hypothesis
            refs = [*candidate.supporting_evidence_ids, *candidate.refuting_evidence_ids]
            if update.review_evidence_id:
                refs.append(update.review_evidence_id)
            self._require_refs(refs)
            if any(self.graph.get_local_node(GraphNodeId(identity)) is None
                   for identity in candidate.target_node_ids):
                raise StateConflict("candidate_target_missing")
            if any(self.graph.get_local_node(hypothesis_node_id(identity)) is None
                   for identity in candidate.source_hypothesis_ids):
                raise StateConflict("candidate_source_missing")
            if candidate.status in {"confirmed", "rejected"}:
                record = inspected.get(update.review_evidence_id or "")
                meta = record.metadata if record else {}
                expected_outcome = "success" if candidate.status == "confirmed" else "refuted"
                if (update.expected_revision is None or meta.get("record_kind") != "review_receipt"
                        or meta.get("public") is not True or meta.get("admission") != "final"
                        or meta.get("hypothesis_id") != candidate.hypothesis_id
                        or meta.get("hypothesis_outcome", meta.get("outcome")) != expected_outcome
                        or str(meta.get("verdict", "")).lower() == "unknown"
                        or meta.get("candidate_revision") != update.expected_revision
                        or meta.get("candidate_purpose_hash") != content_hash(
                            hypothesis_purpose(candidate))):
                    raise StateConflict("candidate_verdict_receipt_unbound")
            node = self.graph.save_hypothesis(update, now=datetime.now(timezone.utc))
            revisions[candidate.hypothesis_id] = node.properties["facts"]["revision"]
        saved = self.register_batch(request_id, tasks, checked=checked) if tasks else []
        for envelope in notifications:
            self.queue(envelope)
        receipt = {"request_hash": request_hash, "task_ids": [str(task.task_id) for task in saved],
                   "candidate_revisions": revisions, "contract_version": 2}
        self.db.execute("INSERT INTO state_requests VALUES(?,?,?,?)", (
            self.run_id, "hypothesis_probe_registration", request_id, json.dumps(receipt),
        ))
        if planner_judgment is not None:
            self.finish_hypothesis_judgment(request_id, planner_judgment)
        return receipt

    def check_planning_inputs(self, revision, binding, graph_read, task_read=None) -> None:
        from redteam.core.ids import GraphNodeId

        if self.admit().revision != revision or self.binding()[1] != binding:
            raise PlanningInputsChanged("planning_goal_or_binding_changed")
        if any(content_hash(self.graph.get_local_node(GraphNodeId(identity))) != expected
               for identity, expected in graph_read.items()):
            raise PlanningInputsChanged("candidate_related_graph_changed")
        if any(content_hash(self.get_task(identity)) != expected
               for identity, expected in (task_read or {}).items()):
            raise PlanningInputsChanged("probe_related_result_changed")

    def hypothesis_planner_cursor(self) -> int | None:
        row = self.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? "
            "AND kind='hypothesis_planner_cursor' AND identity=?", (self.run_id, self.run_id),
        ).fetchone()
        return json.loads(row[0])["sequence"] if row else None

    def hypothesis_candidate_inputs(self) -> dict:
        from redteam.attack_graph.predicates import facts_of
        from redteam.core.enums import GraphNodeType

        return {facts_of(node.properties)["hypothesis_id"]: content_hash(facts_of(node.properties))
                for node in self.graph.query(node_type=GraphNodeType.HYPOTHESIS)}

    def check_planner_inventory(self, judgment: dict) -> None:
        if judgment["termination"] == "continue":
            return  # Candidate-local judgments do not fence unrelated siblings.
        if set(judgment["candidate_inventory"]) != self.hypothesis_candidate_inputs().keys() or (
            set(judgment["task_inventory"]) != {str(task.task_id) for task in self.list_tasks()}
        ):
            raise PlanningInputsChanged("planning_terminal_inventory_changed")

    def finish_hypothesis_judgment(self, identity: str, judgment: dict) -> None:
        """Same commit as candidates/Tasks; coalesce only consumed outbox inputs."""
        from redteam.orchestration.central_completion import goal_snapshot

        self.admit()
        payload = redact_secret_material(judgment)
        self.db.execute("INSERT INTO state_requests VALUES(?,?,?,?)", (
            self.run_id, "hypothesis_planner_pass", identity, json.dumps(payload),
        ))
        self.db.execute(
            "INSERT INTO state_requests VALUES(?,'hypothesis_planner_cursor',?,?) "
            "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
            (self.run_id, self.run_id, json.dumps({"sequence": payload["sequence"]})),
        )
        termination = {"termination": payload["termination"], "reason": payload["reason"],
                       "resume_conditions": payload["resume_conditions"],
                       "candidate_inputs": self.hypothesis_candidate_inputs(),
                       "inputs": goal_snapshot(self, include_assessment=False)[2]}
        self.db.execute(
            "INSERT INTO state_requests VALUES(?,'planner_termination',?,?) "
            "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
            (self.run_id, self.run_id, json.dumps(termination)),
        )

    def update_task(
        self, task: MissionTask, *, owner: str, version: int, expected_hash: str,
        checked: Sequence[CheckedEvidence] = (),
    ) -> MissionTask:
        """Compare this Task's inputs only; sibling Task updates do not invalidate it."""
        self.admit()
        previous = self._owned(str(task.task_id), owner, version)
        task = task.redacted_copy()
        if (content_hash(previous) != expected_hash or task.mission_id != previous.mission_id
                or task.investigation_contract_version != previous.investigation_contract_version
                or task.probe_limits != previous.probe_limits
                or task.occupant_owner != owner or task.ownership_version != version
                or task.status != previous.status
                or task.blocking_reason != previous.blocking_reason
                or task.result != previous.result or task.dependency_task_ids or task.parent_task_id
                or task.priority != 0):
            raise StateConflict("task_inputs_or_owned_lifecycle_changed")
        if previous.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            raise StateConflict("task_already_terminal")
        if (completion_contract_hash(task) != completion_contract_hash(previous)
                and self._unsafe_execution(str(task.task_id))):
            raise StateConflict("task_purpose_has_unresolved_execution")
        self.add_evidence_refs(checked)
        self._require_refs(task.evidence_ids)
        return self._write_task(task)

    def _unsafe_execution(self, task_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM state_executions WHERE run_id=? AND task_id=? AND "
            "(json_extract(payload,'$.state') NOT IN ('succeeded','failed','cancelled') "
            "OR json_extract(payload,'$.resource_execution_pending')=1 "
            "OR json_extract(payload,'$.wait_reason') IS NOT NULL) LIMIT 1",
            (self.run_id, task_id),
        ).fetchone() is not None

    def _refresh_execution_wait(self, task: MissionTask) -> None:
        if task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            return  # A late original can settle without undoing cancellation.
        row = self.db.execute(
            "SELECT payload FROM state_executions WHERE run_id=? AND task_id=? AND "
            "(json_extract(payload,'$.wait_reason') IS NOT NULL OR "
            "json_extract(payload,'$.state')='unknown') ORDER BY invocation_id LIMIT 1",
            (self.run_id, str(task.task_id)),
        ).fetchone()
        waiting = TaskExecution.model_validate_json(row[0]) if row else None
        self._write_task(task.model_copy(update={
            "status": TaskStatus.BLOCKED if waiting else TaskStatus.RUNNING,
            "blocking_reason": (
                f"{waiting.wait_reason or 'unknown'}:{waiting.action_id}" if waiting else None
            ),
        }))

    def _active_requests(self, owner: str, *, any_activity: bool = False) -> bool:
        sql = "SELECT 1 FROM state_requests WHERE run_id=? AND kind='llm_request' "
        sql += "AND json_extract(payload,'$.owner')=? "
        if not any_activity:
            sql += "AND json_extract(payload,'$.state') IN ('reserved','dispatched') "
        return self.db.execute(sql + "LIMIT 1", (self.run_id, owner)).fetchone() is not None

    def _check_external_activity(self, task_id: str, *, any_activity: bool = False) -> None:
        instances = [row[0] for row in self.db.execute(
            "SELECT instance_id FROM state_activations WHERE run_id=? AND task_id=?",
            (self.run_id, task_id),
        )]
        if self.activity_check is not None:
            if self.activity_check(
                self.db, self.engagement_id, self.run_id, instances, any_activity,
            ):
                raise StateConflict("task_has_unreconciled_network_activity")
        elif instances and self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_windows'",
        ).fetchone():
            raise StateConflict("network_recovery_inspection_not_bound")

    def claim_task(self, task_id: str, *, owner: str, instance_id: str) -> MissionTask:
        self.admit()
        task = self.get_task(task_id)
        if not owner.strip() or not instance_id.strip():
            raise CoordinationError("task_owner_identity_missing")
        if (task is None or task.occupant_owner is not None
                or task.status not in {TaskStatus.PENDING, TaskStatus.READY}
                or self._unsafe_execution(task_id)):
            raise StateConflict("task_not_assignable")
        task = task.model_copy(update={
            "occupant_owner": owner, "ownership_version": task.ownership_version + 1,
            "status": TaskStatus.RUNNING, "updated_at": datetime.now(timezone.utc),
        })
        _, binding_hash = self.binding()
        self.db.execute("INSERT INTO state_activations VALUES(?,?,?,?,?,'reserved',1,?)", (
            self.run_id, instance_id, task_id, owner, task.ownership_version, binding_hash,
        ))
        return self._write_task(task)

    def entering(self, task_id: str, *, owner: str, version: int, instance_id: str) -> None:
        self.admit()
        self._owned(task_id, owner, version)
        _, binding_hash = self.binding()
        updated = self.db.execute(
            "UPDATE state_activations SET launch_stage='entered' WHERE run_id=? AND instance_id=? "
            "AND task_id=? AND owner=? AND ownership_version=? AND proof_version=1 "
            "AND binding_hash=? AND launch_stage IN ('reserved','entered')",
            (self.run_id, instance_id, task_id, owner, version, binding_hash),
        )
        if updated.rowcount != 1:
            raise StateConflict("activation_original_binding_mismatch")

    def release_task(self, task_id: str, *, owner: str, version: int) -> MissionTask:
        task = self._owned(task_id, owner, version)
        self._check_external_activity(task_id)
        if self._unsafe_execution(task_id) or self._active_requests(owner):
            raise StateConflict("task_has_unresolved_activity")
        if task.status is not TaskStatus.RUNNING:
            raise StateConflict("task_not_running")
        self.db.execute(
            "UPDATE state_activations SET launch_stage='returned' "
            "WHERE run_id=? AND task_id=? AND ownership_version=?",
            (self.run_id, task_id, version),
        )
        return self._write_task(task.model_copy(update={
            "occupant_owner": None, "ownership_version": version + 1,
            "status": TaskStatus.READY, "updated_at": datetime.now(timezone.utc),
        }))

    def adjust_task_necessity(
        self, task_id: str, *, action: str, required=None, reason="", goal_revision=None,
        owner=None, version=None, criterion_refs=(), evidence_ids=(),
        source_task_id=None, source_result_id=None, source_question_id=None,
    ) -> MissionTask:
        self.admit()
        mission_id, _ = self.binding()
        mission = self.get_mission(mission_id)
        if mission is None:
            raise StateConflict("planning_mission_missing")
        if goal_revision is not None and mission.revision != goal_revision:
            raise StateConflict("planning_goal_changed")
        task = self.get_task(task_id)
        if task is None or str(task.mission_id) != mission_id:
            raise StateConflict("planning_task_scope_mismatch")
        if task.status not in {TaskStatus.PENDING, TaskStatus.READY}:
            raise StateConflict("task_necessity_requires_unstarted_task")
        if task.result is not None:
            raise StateConflict("task_necessity_result_already_recorded")
        if task.occupant_owner is not None:
            if owner != task.occupant_owner or version != task.ownership_version:
                raise StateConflict("stale_task_ownership")
            if self._active_requests(owner):
                raise StateConflict("task_has_unresolved_activity")
        elif owner is not None or version is not None:
            raise StateConflict("stale_task_ownership")
        if self._unsafe_execution(task_id):
            raise StateConflict("task_has_unresolved_execution")
        if self.db.execute(
            "SELECT 1 FROM state_activations WHERE run_id=? AND task_id=? LIMIT 1",
            (self.run_id, task_id),
        ).fetchone() is not None:
            raise StateConflict("task_has_unreconciled_activation")
        if not reason.strip():
            raise CoordinationError("task_necessity_reason_required")
        criteria = [c for c in (criterion_refs or []) if str(c).strip()]
        evidence = [e for e in (evidence_ids or []) if str(e).strip()]
        if set(criteria) - set(mission.success_criteria):
            raise StateConflict("planner_adjustment_unknown_criterion")
        if evidence:
            self._require_refs(evidence)
        if source_task_id is not None:
            source = self.get_task(str(source_task_id))
            if (
                source is None or str(source.mission_id) != mission_id
                or source.result is None
                or source.result.result_id != source_result_id
                or str(source_question_id) not in {
                    q.question_id for q in source.result.questions
                }
            ):
                raise StateConflict("planner_adjustment_bad_result_link")
        if not (criteria or evidence or source_task_id is not None):
            raise StateConflict("planner_adjustment_ungrounded")
        if action == "cancel":
            update = {"status": TaskStatus.CANCELLED, "required": False}
        elif action == "set_required":
            if required is None or required == task.required:
                raise CoordinationError("task_necessity_no_required_change")
            update = {"required": required}
        else:
            raise CoordinationError("task_necessity_unknown_action")
        metadata = dict(task.provenance.metadata)
        if "original_required" not in metadata:
            metadata["original_required"] = task.required
        history = list(metadata.get("necessity_adjustments", []))
        history.append({
            "action": action, "reason": reason.strip()[:500], "goal_revision": mission.revision,
            "criteria": criteria[:8], "evidence": evidence[:8],
            "source": (
                {
                    "task_id": str(source_task_id), "result_id": source_result_id,
                    "question_id": source_question_id,
                }
                if source_task_id is not None else None
            ),
        })
        metadata["necessity_adjustments"] = history[-10:]
        provenance = task.provenance.model_copy(update={"metadata": metadata})
        return self._write_task(task.model_copy(update={
            **update, "provenance": provenance, "updated_at": datetime.now(timezone.utc),
        }))

    def recover_unentered(
        self, task_id: str, *, previous_owner: str, version: int, owner: str, instance_id: str,
    ) -> MissionTask:
        """Only a proven never-entered activation; entered recovery belongs to plan2."""
        self.admit()
        self._owned(task_id, previous_owner, version)
        self._check_external_activity(task_id, any_activity=True)
        _, binding_hash = self.binding()
        proof = self.db.execute(
            "SELECT launch_stage,proof_version,binding_hash FROM state_activations "
            "WHERE run_id=? AND task_id=? AND owner=? AND ownership_version=?",
            (self.run_id, task_id, previous_owner, version),
        ).fetchall()
        if (proof != [("reserved", 1, binding_hash)] or self._unsafe_execution(task_id)
                or self._active_requests(previous_owner, any_activity=True)):
            raise StateConflict("never_entered_recovery_not_proven")
        self.release_task(task_id, owner=previous_owner, version=version)
        return self.claim_task(task_id, owner=owner, instance_id=instance_id)

    def finish_task(
        self, task_id: str, result: InvestigationResult, *, owner: str, version: int,
        checked: Sequence[CheckedEvidence] = (),
    ) -> MissionTask:
        task = self._owned(task_id, owner, version)
        result = InvestigationResult.model_validate(redact_secret_material(
            result.model_dump(mode="json"),
        ))
        self._check_result_lineage(task, result)
        if task.status is TaskStatus.COMPLETED:
            if task.result != result:
                raise StateConflict("task_result_identity_conflict")
            return task
        self._check_external_activity(task_id)
        if task.status is not TaskStatus.RUNNING or self._unsafe_execution(task_id):
            raise StateConflict("task_has_unresolved_execution")
        if self._active_requests(owner):
            raise StateConflict("task_has_active_request")
        self.add_evidence_refs(checked)
        refs = list(result.evidence_ids)
        refs += [ref for question in result.questions for ref in question.evidence_ids]
        if result.completion_receipt_id is not None:
            refs.append(result.completion_receipt_id)
        self._require_refs(refs)
        # Retain the completing owner/generation for exact result replay. It
        # cannot run more actions once status is completed, nor be re-claimed.
        return self._write_task(task.model_copy(update={
            "result": result, "status": TaskStatus.COMPLETED,
            "completion_evidence_id": result.completion_receipt_id,
            "updated_at": datetime.now(timezone.utc),
        }))

    def publish_result(
        self, task_id: str, result: InvestigationResult, *, owner: str, version: int,
        expected_hash: str, checked: Sequence[CheckedEvidence] = (),
    ) -> MissionTask:
        """An intermediate result stays on its running Task, without a question queue."""
        self.admit()
        task = self._owned(task_id, owner, version)
        allowed = {TaskStatus.RUNNING}
        if task.investigation_contract_version == 2:
            allowed.add(TaskStatus.BLOCKED)
        if task.status not in allowed or content_hash(task) != expected_hash:
            raise StateConflict("task_result_inputs_changed")
        result = InvestigationResult.model_validate(redact_secret_material(
            result.model_dump(mode="json"),
        ))
        self._check_result_lineage(task, result)
        self.add_evidence_refs(checked)
        self._require_refs([*result.evidence_ids,
                            *(ref for q in result.questions for ref in q.evidence_ids)])
        return self._write_task(task.model_copy(update={"result": result,
                                  "updated_at": datetime.now(timezone.utc)}))

    def _check_result_lineage(self, task: MissionTask, result: InvestigationResult) -> None:
        if task.investigation_contract_version == 2:
            if result.contract_version != 2 or set(result.hypothesis_ids) - set(
                task.related_hypothesis_ids
            ):
                raise StateConflict("probe_result_contract_or_subject_changed")
        if task.result is None:
            return
        if task.result.result_id != result.result_id:
            raise StateConflict("task_result_identity_conflict")
        previous = {q.question_id: q for q in task.result.questions}
        proposed = {q.question_id: q for q in result.questions}
        if any(proposed.get(identity) != question for identity, question in previous.items()):
            raise StateConflict("original_result_question_changed")
        if task.investigation_contract_version == 2 and (
            set(task.result.evidence_ids) - set(result.evidence_ids)
            or set(task.result.observations) - set(result.observations)
            or set(task.result.limitations) - set(result.limitations)
        ):
            raise StateConflict("original_probe_observation_removed")

    def activation_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM state_activations WHERE run_id=?",
                               (self.run_id,)).fetchone()[0]

    def active_activation_count(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM state_activations WHERE run_id=? AND launch_stage!='returned'",
            (self.run_id,),
        ).fetchone()[0]

    def latest_event_sequence(self, event_types: Sequence[str]) -> int:
        """Read the existing committed outbox cursor; never a graph digest."""
        self.binding()
        return self.db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM state_outbox WHERE run_id=? "
            "AND json_extract(payload,'$.envelope.event_type') IN (SELECT value FROM json_each(?))",
            (self.run_id, json.dumps(list(event_types))),
        ).fetchone()[0]

    def policy_budget_remaining(self) -> int | None:
        """Read original Control decisions for display/context, never admission."""
        self.binding()
        row = self.db.execute(
            "SELECT json_extract(payload,'$.envelope.payload.budget_remaining') "
            "FROM state_outbox WHERE run_id=? "
            "AND json_extract(payload,'$.envelope.event_type')=? "
            "AND json_type(payload,'$.envelope.payload.budget_remaining')='integer' "
            "ORDER BY sequence DESC LIMIT 1",
            (self.run_id, EventType.POLICY_EVALUATED),
        ).fetchone()
        return row[0] if row else None

    def actor_returned(self, task_id: str, *, owner: str, version: int, instance_id: str,
                       failed: bool = False) -> None:
        """Return an actor slot without releasing its Task, calls or reservations."""
        task = self._owned(task_id, owner, version)
        if self._active_requests(owner):
            raise StateConflict("actor_has_active_request")
        _, binding_hash = self.binding()
        updated = self.db.execute(
            "UPDATE state_activations SET launch_stage='returned' WHERE run_id=? AND instance_id=? "
            "AND task_id=? AND owner=? AND ownership_version=? "
            "AND proof_version=1 AND binding_hash=?",
            (self.run_id, instance_id, task_id, owner, version, binding_hash),
        )
        if updated.rowcount != 1:
            raise StateConflict("activation_original_binding_mismatch")
        if failed and task.status is TaskStatus.RUNNING:
            self._write_task(task.model_copy(update={
                "status": TaskStatus.BLOCKED, "blocking_reason": f"needs_review:{instance_id}",
            }))

    def continue_settled_actor(self, task_id: str, *, owner: str, version: int,
                               instance_id: str, max_instances: int) -> None:
        """Called only after the runner verifies the original checkpoint and calls."""
        self.admit()
        task = self._owned(task_id, owner, version)
        if (task.status is not TaskStatus.RUNNING or self._unsafe_execution(task_id)
                or self._active_requests(owner)):
            raise StateConflict("actor_continuation_unresolved")
        self._check_external_activity(task_id)
        original = self.db.execute(
            "SELECT launch_stage FROM state_activations WHERE run_id=? AND instance_id=?",
            (self.run_id, instance_id),
        ).fetchone()
        if original is None:
            raise StateConflict("activation_original_binding_mismatch")
        if self.active_activation_count() + int(original[0] == "returned") > max_instances:
            raise StateConflict("central_worker_limit_reached")
        _, binding_hash = self.binding()
        updated = self.db.execute(
            "UPDATE state_activations SET launch_stage='entered' WHERE run_id=? AND instance_id=? "
            "AND task_id=? AND owner=? AND ownership_version=? AND proof_version=1 "
            "AND binding_hash=? AND launch_stage IN ('entered','returned')",
            (self.run_id, instance_id, task_id, owner, version, binding_hash),
        )
        if updated.rowcount != 1:
            raise StateConflict("activation_original_binding_mismatch")

    def recover_actor(self, task_id: str, *, previous_owner: str, version: int,
                      instance_id: str, owner: str,
                      checked: Sequence[CheckedEvidence],
                      active_jobs: Mapping[str, str] | None = None) -> MissionTask:
        """CAS handoff of a verified original actor, never a new investigation.

        Caller holds exclusive Run ownership, checks its latest SDK snapshot,
        collects originals and settles lost requests before this transaction.
        Executions keep their original ownership epoch and invocation identity.
        """
        self.admit()
        task = self._owned(task_id, previous_owner, version)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            raise StateConflict("recovery_task_already_terminal")
        if not owner or owner == previous_owner or self._active_requests(previous_owner):
            raise StateConflict("recovery_owner_not_quiescent")
        _, binding_hash = self.binding()
        original = self.db.execute(
            "SELECT task_id,owner,ownership_version,proof_version,binding_hash "
            "FROM state_activations WHERE run_id=? AND instance_id=?",
            (self.run_id, instance_id),
        ).fetchone()
        if original is None or tuple(original) != (task_id, previous_owner, version, 1,
                                                   binding_hash):
            raise StateConflict("activation_original_binding_mismatch")
        originals = {str(item.record.evidence_id): item.record for item in checked}
        executions = self.db.execute("SELECT payload FROM state_executions "
                                     "WHERE run_id=? AND task_id=?", (self.run_id, task_id))
        for row in executions:
            execution = TaskExecution.model_validate_json(row[0])
            receipt = originals.get(str(execution.proposal_evidence_id))
            if execution.instance_id != instance_id or receipt is None:
                raise StateConflict("recovery_original_execution_unverified")
            check_execution_receipt(execution, receipt, result=False, task=task)
            if execution.state in {ExecutionState.RUNNING, ExecutionState.UNKNOWN}:
                if (not execution.job_ref or active_jobs is None
                        or active_jobs.get(str(execution.invocation_id)) != execution.job_ref):
                    raise StateConflict("recovery_execution_unresolved")
        self.add_evidence_refs(checked)
        # Unclosed network windows are held; handoff cannot make a new epoch
        # while an original provider may still deliver traffic.
        self._check_external_activity(task_id)
        self.db.execute("UPDATE state_activations SET owner=?,ownership_version=?,"
                        "launch_stage='returned' WHERE run_id=? AND instance_id=?",
                        (owner, version + 1, self.run_id, instance_id))
        return self._write_task(task.model_copy(update={
            "occupant_owner": owner, "ownership_version": version + 1,
            "updated_at": datetime.now(timezone.utc),
        }))

    def resume_actor(self, task_id: str, *, owner: str, version: int, instance_id: str,
                     max_instances: int) -> None:
        """Reserve the original actor slot for explicit recovery, including waits."""
        self.admit()
        task = self._owned(task_id, owner, version)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            raise StateConflict("recovery_task_already_terminal")
        if self._active_requests(owner):
            raise StateConflict("actor_continuation_unresolved")
        _, binding_hash = self.binding()
        proof = self.db.execute("SELECT launch_stage FROM state_activations WHERE run_id=? "
                                "AND instance_id=? AND owner=? AND ownership_version=? "
                                "AND binding_hash=? AND proof_version=1",
                                (self.run_id, instance_id, owner, version, binding_hash)).fetchone()
        if proof is None:
            raise StateConflict("activation_original_binding_mismatch")
        if self.active_activation_count() + int(proof[0] == "returned") > max_instances:
            raise StateConflict("central_worker_limit_reached")
        self.db.execute("UPDATE state_activations SET launch_stage='entered' "
                        "WHERE run_id=? AND instance_id=?", (self.run_id, instance_id))
        if (task.status is TaskStatus.BLOCKED
                and str(task.blocking_reason).startswith("needs_review:")
                and not self._unsafe_execution(task_id)):
            self._write_task(task.model_copy(update={"status": TaskStatus.RUNNING,
                                                    "blocking_reason": None}))

    def resume_execution(self, invocation_id: str, *, owner: str, version: int,
                         now: float, ttl: float, max_calls: int, per_target: int,
                         approval: Approval | None = None,
                         comparison_keys: Sequence[str] | None = None) -> TaskExecution:
        """An original never-entered intent, after Control revalidation."""
        self.admit()
        execution = self.get_execution(invocation_id)
        if execution is None or execution.state is not ExecutionState.PENDING:
            raise StateConflict("central_execution_cannot_be_replayed")
        task = self._owned(str(execution.task_id), owner, version)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            raise StateConflict("recovery_task_already_terminal")
        if execution.approval_ref is not None:
            if (approval is None or approval.status is not ApprovalStatus.APPROVED
                    or str(approval.approval_id) != execution.approval_ref
                    or approval.action_id != execution.action_id
                    or approval.execution_fingerprint != execution.approval_fingerprint):
                raise StateConflict("approval_original_proposal_mismatch")
        self.check_execution_capacity(execution, max_calls=max_calls, per_target=per_target)
        self._resource_conflict(execution, now=now, comparison_keys=comparison_keys)
        execution = execution.model_copy(update={"wait_reason": None,
                                                "resource_lease_until": now + ttl})
        self.db.execute("UPDATE state_executions SET payload=? WHERE run_id=? "
                        "AND invocation_id=?", (execution.model_dump_json(), self.run_id,
                                               invocation_id))
        self._refresh_execution_wait(task)
        return execution

    def wait_for_resource(self, invocation_id: str, *, owner: str, version: int) -> None:
        """Keep the exact pending intent/approval/charge after transient contention."""
        self.admit()
        execution = self.get_execution(invocation_id)
        if execution is None or execution.state is not ExecutionState.PENDING:
            raise StateConflict("central_execution_cannot_be_replayed")
        task = self._owned(str(execution.task_id), owner, version)
        execution = execution.model_copy(update={"wait_reason": "resource",
                                                "resource_lease_until": 0})
        self.db.execute("UPDATE state_executions SET payload=? WHERE run_id=? AND invocation_id=?",
                        (execution.model_dump_json(), self.run_id, invocation_id))
        self._refresh_execution_wait(task)

    def reserve_resource_actor(self, task_id: str, *, owner: str, version: int,
                               instance_id: str, now: float, max_instances: int,
                               max_calls: int, per_target: int,
                               comparison_keys: Mapping[str, Sequence[str]] | None = None) -> bool:
        """Live continuation of a returned actor, never owner transfer or replay."""
        self.admit()
        task = self._owned(task_id, owner, version)
        if (task.status is not TaskStatus.BLOCKED
                or not str(task.blocking_reason).startswith("resource:")):
            return False
        proof = self.db.execute("SELECT launch_stage FROM state_activations WHERE run_id=? "
                                "AND instance_id=? AND owner=? AND ownership_version=?",
                                (self.run_id, instance_id, owner, version)).fetchone()
        if proof != ("returned",) or self.active_activation_count() >= max_instances:
            return False
        executions = [TaskExecution.model_validate_json(row[0]) for row in self.db.execute(
            "SELECT payload FROM state_executions WHERE run_id=? AND task_id=?",
            (self.run_id, task_id),
        )]
        pending = [item for item in executions if item.state is ExecutionState.PENDING
                   and item.wait_reason == "resource"]
        if not pending or any(execution_occupies_capacity(item)
                              or (item.wait_reason not in {None, "resource"})
                              or (item.state not in _RESOLVED and item not in pending)
                              for item in executions):
            return False
        if self._active_requests(owner):
            return False
        try:
            self._check_external_activity(task_id)
        except StateConflict as exc:
            if str(exc) == "task_has_unreconciled_network_activity":
                return False
            raise
        for execution in pending:
            try:
                self.check_execution_capacity(execution, max_calls=max_calls, per_target=per_target)
                self._resource_conflict(
                    execution, now=now,
                    comparison_keys=comparison_keys.get(str(execution.invocation_id))
                    if comparison_keys is not None else None,
                )
            except StateConflict as exc:
                if str(exc) in {"shared_resource_execution_conflict",
                                "central_tool_capacity_occupied",
                                "central_target_capacity_occupied"}:
                    return False
                raise
        self.resume_actor(task_id, owner=owner, version=version, instance_id=instance_id,
                          max_instances=max_instances)
        return True

    def hold_resource_actor(self, task_id: str, *, owner: str, version: int) -> None:
        task = self._owned(task_id, owner, version)
        if (task.status is TaskStatus.BLOCKED
                and str(task.blocking_reason).startswith("resource:")):
            self._write_task(task.model_copy(update={
                "blocking_reason": "needs_review:resource_original_unverified",
            }))

    def check_execution_capacity(self, execution: TaskExecution, *, max_calls: int,
                                 per_target: int) -> None:
        occupied = [TaskExecution.model_validate_json(row[0]) for row in self.db.execute(
            "SELECT payload FROM state_executions WHERE run_id=? AND invocation_id!=?",
            (self.run_id, str(execution.invocation_id)),
        )]
        occupied = [record for record in occupied if execution_occupies_capacity(record)]
        if len(occupied) >= max_calls:
            raise StateConflict("central_tool_capacity_occupied")
        if execution.target_identity_verified and execution.target_node_id is None:
            return  # Verified target-free calls consume only the global tool bound.
        if sum(not record.target_identity_verified
               or record.target_node_id == execution.target_node_id
               for record in occupied) >= per_target:
            raise StateConflict("central_target_capacity_occupied")

    def get_execution(self, invocation_id: str) -> TaskExecution | None:
        self.binding()
        row = self.db.execute(
            "SELECT payload FROM state_executions WHERE run_id=? AND invocation_id=?",
            (self.run_id, invocation_id),
        ).fetchone()
        return TaskExecution.model_validate_json(row[0]) if row else None

    def _resource_conflict(self, execution: TaskExecution, *, now: float,
                           pre_entry_approval: bool = False,
                           comparison_keys: Sequence[str] | None = None) -> None:
        # Current verified original bindings also protect old pending rows
        # without rewriting their immutable identity/resource-key payload.
        effective_keys = sorted(set(execution.resource_keys) | set(comparison_keys or ()))
        if not effective_keys:
            return
        keys = [key for key in effective_keys
                if not pre_entry_approval or not key.startswith(INVOCATION_RESOURCE_PREFIX)]
        conflict = self.db.execute(
            "SELECT 1 FROM state_execution_resources r JOIN state_executions e "
            "ON e.run_id=r.run_id AND e.invocation_id=r.invocation_id "
            "WHERE r.resource_key IN (SELECT value FROM json_each(?)) "
            "AND NOT (e.run_id=? AND e.invocation_id=?) "
            "AND NOT (substr(r.resource_key,1,?)=? AND "
            "json_extract(e.payload,'$.state')='pending' AND "
            "coalesce(json_extract(e.payload,'$.wait_reason'),'')='approval') AND "
            "(json_extract(e.payload,'$.resource_execution_pending')=1 OR "
            "json_extract(e.payload,'$.state')='unknown' OR "
            "json_extract(e.payload,'$.job_ref') IS NOT NULL OR "
            "json_extract(e.payload,'$.resource_lease_until')>? OR "
            "(substr(r.resource_key,1,?)=? AND "
            "json_extract(e.payload,'$.state')='pending' AND "
            "coalesce(json_extract(e.payload,'$.wait_reason'),'')='')) LIMIT 1",
            (json.dumps(keys), self.run_id, str(execution.invocation_id),
             len(INVOCATION_RESOURCE_PREFIX), INVOCATION_RESOURCE_PREFIX, now,
             len(INVOCATION_RESOURCE_PREFIX), INVOCATION_RESOURCE_PREFIX),
        ).fetchone()
        legacy_hold = self.db.execute(
            "SELECT 1 FROM state_migrated_runs m, json_each(m.identity,'$.held_resource_keys') k "
            "WHERE k.value IN (SELECT value FROM json_each(?)) LIMIT 1",
            (json.dumps(keys),),
        ).fetchone()
        # Old rows have no physical key/principal proof. Hold matching tool/input
        # while unresolved; a new ID cannot prove an independent old context.
        legacy_exact = None
        if (not pre_entry_approval and any(
                key.startswith(INVOCATION_RESOURCE_PREFIX) for key in effective_keys)):
            legacy_exact = self.db.execute(
                "SELECT 1 FROM state_executions e WHERE run_id=? AND invocation_id!=? "
                "AND json_extract(payload,'$.tool_id')=? "
                "AND json_extract(payload,'$.input_digest')=? "
                "AND (json_extract(payload,'$.state') IN ('running','unknown') OR "
                "json_extract(payload,'$.job_ref') IS NOT NULL OR "
                "json_extract(payload,'$.resource_execution_pending')=1 OR "
                "(json_extract(payload,'$.state')='pending' AND "
                "json_extract(payload,'$.wait_reason') IS NULL)) "
                "AND NOT EXISTS (SELECT 1 FROM json_each(e.payload,'$.resource_keys') k "
                "WHERE substr(k.value,1,?)=?) LIMIT 1",
                (self.run_id, str(execution.invocation_id), str(execution.tool_id),
                 execution.input_digest, len(INVOCATION_RESOURCE_PREFIX),
                 INVOCATION_RESOURCE_PREFIX),
            ).fetchone()
        if conflict or legacy_hold or legacy_exact:
            raise StateConflict("shared_resource_execution_conflict")

    def record_execution(
        self, execution: TaskExecution, *, owner: str, checked: Sequence[CheckedEvidence],
        now: float, max_calls: int | None = None, per_target: int | None = None,
    ) -> TaskExecution:
        execution = TaskExecution.model_validate(redact_secret_material(
            execution.model_dump(mode="json"),
        ))
        task = self._owned(str(execution.task_id), owner, execution.ownership_version)
        if (str(execution.run_id) != self.run_id
                or str(execution.engagement_id) != self.engagement_id):
            raise StateConflict("execution_scope_mismatch")
        previous = self.get_execution(str(execution.invocation_id))
        if previous is not None:
            if execution_identity_hash(previous) != execution_identity_hash(execution):
                raise StateConflict("execution_identity_conflict")
            return previous
        if task.probe_limits is not None and self.probe_usage(str(task.task_id))[
            "actions"
        ] >= task.probe_limits.max_actions:
            raise StateConflict("probe_action_limit_exhausted")
        mission = self.admit()
        if execution.goal_revision != mission.revision:
            raise StateConflict("execution_goal_revision_changed")
        duplicate = self.db.execute(
            "SELECT 1 FROM state_executions WHERE run_id=? AND "
            "(json_extract(payload,'$.action_id')=? OR "
            "(json_extract(payload,'$.instance_id')=? AND "
            "json_extract(payload,'$.model_tool_call_id')=?)) LIMIT 1",
            (self.run_id, str(execution.action_id), str(execution.instance_id),
             execution.model_tool_call_id),
        ).fetchone()
        if duplicate:
            raise StateConflict("execution_call_identity_already_registered")
        if (task.status is not TaskStatus.RUNNING or execution.state is not ExecutionState.PENDING
                or execution.resource_execution_pending or execution.wait_reason == "job"):
            raise StateConflict("execution_requires_current_task_and_pre_entry_intent")
        activation = self.db.execute(
            "SELECT 1 FROM state_activations WHERE run_id=? AND instance_id=? AND task_id=? "
            "AND owner=? AND ownership_version=? AND launch_stage='entered'",
            (self.run_id, str(execution.instance_id), str(execution.task_id), owner,
             execution.ownership_version),
        ).fetchone()
        if not activation:
            raise StateConflict("execution_instance_not_entered")
        self.add_evidence_refs(checked)
        self._require_refs([execution.proposal_evidence_id])
        proposal = next((item.record for item in checked
                         if item.record.evidence_id == execution.proposal_evidence_id), None)
        if proposal is None:
            raise StateConflict("execution_original_proposal_mismatch")
        check_execution_receipt(execution, proposal, result=False, task=task)
        if (execution.wait_reason != "resource" and execution.resource_keys
                and execution.resource_lease_until <= now):
            raise StateConflict("execution_resource_reservation_expired")
        if execution.wait_reason != "resource":
            self._resource_conflict(execution, now=now,
                                    pre_entry_approval=execution.wait_reason == "approval")
        if (execution.wait_reason is None and max_calls is not None and per_target is not None):
            try:
                self.check_execution_capacity(execution, max_calls=max_calls, per_target=per_target)
            except StateConflict:
                execution = execution.model_copy(
                    update={"wait_reason": "resource", "resource_lease_until": 0},
                )
        self.db.execute("INSERT INTO state_executions VALUES(?,?,?,?,?)", (
            self.run_id, str(execution.invocation_id), str(execution.task_id),
            execution_identity_hash(execution), execution.model_dump_json(),
        ))
        self.db.executemany("INSERT INTO state_execution_resources VALUES(?,?,?)", (
            (key, self.run_id, str(execution.invocation_id)) for key in execution.resource_keys
        ))
        if execution.wait_reason in {"approval", "resource"}:
            self._write_task(task.model_copy(update={
                "status": TaskStatus.BLOCKED,
                "blocking_reason": f"{execution.wait_reason}:{execution.action_id}",
            }))
        return execution

    def update_execution(
        self, execution: TaskExecution, *, owner: str, checked: Sequence[CheckedEvidence] = (),
        now: float, ownership_version: int | None = None, admitted_revision: int | None = None,
        comparison_keys: Sequence[str] | None = None,
    ) -> TaskExecution:
        execution = TaskExecution.model_validate(redact_secret_material(
            execution.model_dump(mode="json"),
        ))
        task = self._owned(str(execution.task_id), owner,
                           ownership_version or execution.ownership_version)
        previous = self.get_execution(str(execution.invocation_id))
        if (previous is None
                or execution_identity_hash(previous) != execution_identity_hash(execution)):
            raise StateConflict("execution_identity_conflict")
        if previous == execution:
            return previous
        if previous.state in _RESOLVED and previous.wait_reason != "job":
            raise StateConflict("execution_already_resolved")
        if previous.state is ExecutionState.UNKNOWN and execution.state not in _RESOLVED:
            raise StateConflict("unknown_execution_cannot_be_replayed")
        if execution.state is ExecutionState.PENDING:
            raise StateConflict("execution_cannot_rewind")
        if previous.job_ref is not None and execution.job_ref not in (None, previous.job_ref):
            raise StateConflict("execution_original_job_changed")
        if (execution.state is ExecutionState.RUNNING
                and previous.state is not ExecutionState.RUNNING):
            if task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}:
                raise StateConflict("execution_task_already_terminal")
            mission = self.admit()
            if (admitted_revision or execution.goal_revision) != mission.revision:
                raise StateConflict("execution_goal_revision_changed")
            if previous.state is not ExecutionState.PENDING or previous.wait_reason is not None:
                raise StateConflict("execution_entry_not_admitted")
            self._resource_conflict(execution, now=now, comparison_keys=comparison_keys)
            if execution.resource_keys and execution.resource_lease_until <= now:
                raise StateConflict("execution_resource_reservation_expired")
        self.add_evidence_refs(checked)
        if execution.result_evidence_id is not None:
            original = next((item.record for item in checked
                             if item.record.evidence_id == execution.result_evidence_id), None)
            if original is None:
                raise StateConflict("execution_original_result_mismatch")
            check_execution_receipt(execution, original, result=True)
            self._require_refs([execution.result_evidence_id])
        if execution.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
            if execution.result_evidence_id is None:
                raise StateConflict("execution_resolution_requires_original_receipt")
        if (execution.state is ExecutionState.CANCELLED
                and previous.state is not ExecutionState.PENDING
                and execution.result_evidence_id is None):
            execution.state = ExecutionState.UNKNOWN
        pending = bool(execution.resource_keys) and (
            execution.state in {ExecutionState.RUNNING, ExecutionState.UNKNOWN}
            or execution.wait_reason == "job"
            or (previous.resource_execution_pending and execution.result_evidence_id is None)
        )
        execution.resource_execution_pending = pending
        if execution.state in _RESOLVED and not pending:
            execution.resource_lease_until = 0
        self.db.execute("UPDATE state_executions SET payload=? "
                        "WHERE run_id=? AND invocation_id=?", (
            execution.model_dump_json(), self.run_id, str(execution.invocation_id),
        ))
        self._refresh_execution_wait(task)
        return execution

    def resolve_approval(
        self, invocation_id: str, *, owner: str, version: int, approval: Approval,
    ) -> TaskExecution:
        """Record the original Control approval; admission still precedes provider entry."""
        self.admit()
        execution = self.get_execution(invocation_id)
        if execution is None:
            raise StateConflict("execution_identity_conflict")
        task = self._owned(str(execution.task_id), owner, version)
        if (execution.ownership_version != version or execution.state is not ExecutionState.PENDING
                or task.status not in {TaskStatus.RUNNING, TaskStatus.BLOCKED}
                or str(approval.approval_id) != execution.approval_ref
                or approval.action_id != execution.action_id
                or approval.status is not ApprovalStatus.APPROVED
                or approval.execution_fingerprint != execution.approval_fingerprint):
            raise StateConflict("approval_original_proposal_mismatch")
        execution.wait_reason = None
        self.db.execute("UPDATE state_executions SET payload=? "
                        "WHERE run_id=? AND invocation_id=?", (
            execution.model_dump_json(), self.run_id, invocation_id,
        ))
        self._refresh_execution_wait(task)
        return execution

    def get_intent(self, intent_id: str) -> IntentRecord | None:
        self.binding()
        row = self.db.execute(
            "SELECT payload FROM state_intents WHERE run_id=? AND intent_id=?",
            (self.run_id, intent_id),
        ).fetchone()
        return sanitize_intent_record(json.loads(row[0])) if row else None

    def save_intent(self, record: IntentRecord) -> IntentRecord:
        record = sanitize_intent_record(record)
        mission_id, _ = self.binding()
        if str(record.run_id) != self.run_id or str(record.intent.mission_id) != mission_id:
            raise StateConflict("intent_scope_mismatch")
        old = self.get_intent(str(record.intent_id))
        if old is not None:
            record = check_intent_update(old, record)
        elif record.status is not IntentStatus.RECEIVED:
            raise StateConflict("intent_must_be_received_first")
        self.db.execute(
            "INSERT INTO state_intents VALUES(?,?,?,?,?) ON CONFLICT(run_id,intent_id) DO UPDATE "
            "SET status=excluded.status,payload=excluded.payload",
            (self.run_id, str(record.intent_id), record.status.value,
             record.intent.created_at.isoformat(), record.model_dump_json()),
        )
        return record

    def apply_intent(
        self, record: IntentRecord, mutate: Callable[[CentralTransaction], None],
    ) -> IntentRecord:
        """Existing intent semantics supply the mutation; original+effect commit once.

        This is not the legacy IntentService's second history projection. The
        consumer in plan2 supplies pause/stop/exclude domain effects here.
        """
        saved = self.get_intent(str(record.intent_id))
        if saved is None:
            raise StateConflict("intent_not_received")
        checked = check_intent_update(saved, record)
        if saved.status is not IntentStatus.RECEIVED:
            return checked
        if checked.status is IntentStatus.RECEIVED:
            raise StateConflict("intent_application_requires_final_status")
        if checked.status is IntentStatus.APPLIED:
            mutate(self)
        return self.save_intent(checked)

    def queue(self, envelope: EventEnvelope) -> None:
        envelope = EventEnvelope.model_validate(envelope.model_dump(mode="json"))
        self.binding()
        if str(envelope.run_id) != self.run_id or str(envelope.engagement_id) != self.engagement_id:
            raise StateConflict("event_scope_mismatch")
        old = self.db.execute(
            "SELECT payload FROM state_outbox WHERE run_id=? AND event_id=?",
            (self.run_id, str(envelope.event_id)),
        ).fetchone()
        if old:
            if json.loads(old[0])["envelope"] != envelope.model_dump(mode="json"):
                raise StateConflict("event_identity_conflict")
            return
        sequence = self.db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM state_outbox WHERE run_id=?", (self.run_id,),
        ).fetchone()[0]
        original = self.db.execute(
            "SELECT json_extract(identity,'$.outbox_sequence') FROM state_migrated_runs "
            "WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        if original is not None and original[0] is not None:
            sequence = max(sequence, original[0] + 1)
        # The existing outbox row contract, not a new event protocol.
        payload = {"sequence": sequence, "envelope": envelope.model_dump(mode="json"),
                   "delivered": False, "owner": None, "lease_until": 0.0}
        self.db.execute("INSERT INTO state_outbox VALUES(?,?,?,?)", (
            self.run_id, str(envelope.event_id), sequence, json.dumps(payload),
        ))


class _RequestTransaction:
    """Existing RunRequestLedger view; only this Run's request records are loaded."""

    def __init__(
        self, tx: CentralTransaction, records: MutableMapping[tuple[str, str], Any],
    ) -> None:
        self.tx, self.records = tx, records

    def admit(self, run_id: str, mission_id: str, *, token: str | None = None) -> None:
        if run_id != self.tx.run_id or mission_id != self.tx.binding()[0] or token is not None:
            raise CoordinationError("request_run_binding_mismatched")
        # Reasoning for the owned final review still uses the shared ledger;
        # task assignment/provider admission is closed independently.
        self.tx.admit(settlement=True)

    def admit_probe_request(self, task_id: str, rows: Sequence[dict]) -> None:
        task = self.tx.get_task(task_id)
        if task is None or task.probe_limits is None:
            raise RequestAdmissionError("probe_request_task_unbound")
        if task.status is not TaskStatus.RUNNING:
            raise RequestAdmissionError("probe_task_not_running")
        charged = sum(row.get("task_id") == task_id and row["state"] != "released" for row in rows)
        if charged >= task.probe_limits.max_model_requests:
            raise RequestAdmissionError("probe_model_request_limit_exhausted")


class _RequestRecords(MutableMapping[tuple[str, str], Any]):
    """RunRequestLedger's SQL records: point reads are lazy, inventories Run-scoped."""

    def __init__(self, tx: CentralTransaction) -> None:
        self.tx = tx
        self.loaded: dict[tuple[str, str], Any] = {}
        self.original: dict[tuple[str, str], str | None] = {}

    def __getitem__(self, key: tuple[str, str]) -> Any:
        kind, identity = key
        if kind == "binding" and identity == self.tx.run_id:
            return {"mission_id": self.tx.binding()[0], "engagement_id": self.tx.engagement_id}
        if key not in self.loaded:
            row = self.tx.db.execute(
                "SELECT payload FROM state_requests WHERE run_id=? AND kind=? AND identity=?",
                (self.tx.run_id, kind, identity),
            ).fetchone()
            if row is None:
                raise KeyError(key)
            payload = json.loads(row[0])
            self._check(key, payload)
            self.loaded[key] = payload
            self.original[key] = json.dumps(payload, sort_keys=True)
        return self.loaded[key]

    def _check(self, key: tuple[str, str], payload: Any) -> None:
        kind, identity = key
        if kind == "llm_request":
            RunRequestLedger.checked_record(identity, payload)
            if payload["run_id"] != self.tx.run_id:
                raise RequestAdmissionError("llm_request_record_invalid")
        elif (kind not in {"llm_request_policy", "llm_provider_cooldown"}
              or identity != self.tx.run_id):
            raise RequestAdmissionError("llm_request_record_invalid")

    def __setitem__(self, key: tuple[str, str], value: Any) -> None:
        self._check(key, value)
        if key not in self.original:
            try:
                self[key]
            except KeyError:
                self.original[key] = None
        self.loaded[key] = value

    def __delitem__(self, key: tuple[str, str]) -> None:
        raise RequestAdmissionError("llm_request_history_cannot_be_deleted")

    def __iter__(self) -> Iterator[tuple[str, str]]:
        keys = set(self.tx.db.execute(
            "SELECT kind,identity FROM state_requests WHERE run_id=? "
            "AND kind IN ('llm_request','llm_request_policy','llm_provider_cooldown')",
            (self.tx.run_id,),
        ))
        return iter(keys | self.loaded.keys())

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def flush(self) -> None:
        for (kind, identity), payload in self.loaded.items():
            self._check((kind, identity), payload)
            serialized = json.dumps(payload, sort_keys=True)
            if self.original[(kind, identity)] != serialized:
                self.tx.db.execute(
                    "INSERT INTO state_requests VALUES(?,?,?,?) "
                    "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
                    (self.tx.run_id, kind, identity, serialized),
                )


class CentralStateStore:
    """Explicitly scoped storage selection; it cannot run an agent or open a provider."""

    def __init__(
        self, path: str | Path, *, run_id: str, engagement_id: str,
        activity_check: ActivityCheck | None = None,
    ) -> None:
        if not str(path).strip() or not run_id.strip() or not engagement_id.strip():
            raise ValueError("central storage requires path, Run and Engagement identity")
        self.path = Path(path)
        self.run_id, self.engagement_id = run_id, engagement_id
        self.activity_check = activity_check
        self.read_only = False
        self.original_store_paths: dict[str, str] = {}
        self.migration_permit: ContextVar[MigrationPermit | None] = ContextVar(
            "central_migration_permit", default=None,
        )

    async def setup(self) -> None:
        def initialize() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = connect(self.path, write=True)
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )}
                if "state_schema" not in names and names - {
                    "network_pools", "network_windows", "network_seq_log",
                }:
                    raise StateConflict("central_path_contains_noncentral_storage")
                version = 0
                if "state_schema" in names:
                    rows = db.execute("SELECT version FROM state_schema").fetchall()
                    if len(rows) != 1 or rows[0][0] not in {0, 1, 2, SCHEMA_VERSION}:
                        raise StateConflict("central_schema_version_unsupported")
                    version = rows[0][0]
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                # Recheck under the lock: simultaneous first-open migrations
                # must not both apply the same CREATE statements.
                db.execute("CREATE TABLE IF NOT EXISTS state_schema "
                           "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER)")
                saved = db.execute("SELECT version FROM state_schema WHERE singleton=1").fetchone()
                version = saved[0] if saved else 0
                if version not in {0, 1, 2, SCHEMA_VERSION}:
                    raise StateConflict("central_schema_version_unsupported")
                if version == 0:
                    for statement in _SCHEMA_1:
                        db.execute(statement)
                    version = 1
                if version == 1:
                    for statement in GRAPH_SCHEMA:
                        db.execute(statement)
                    version = 2
                if version == 2:
                    for statement in _SCHEMA_3:
                        db.execute(statement)
                db.execute("INSERT OR REPLACE INTO state_schema VALUES(1,?)", (SCHEMA_VERSION,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        await run_sqlite(initialize)

    async def transaction(
        self, operation: Callable[[CentralTransaction], T], *, write: bool = True,
    ) -> T:
        def execute(db: sqlite3.Connection) -> T:
            result = operation(CentralTransaction(
                db, self.run_id, self.engagement_id, self.activity_check,
                self.migration_permit.get(),
            ))
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("central transactions require synchronous callbacks without I/O")
            return result
        if self.read_only:
            def inspect_original():
                db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    return execute(db)
                finally:
                    db.close()
            return await run_sqlite(inspect_original)
        return await run_transaction(self.path, execute, write=write)

    @classmethod
    async def inspect_run(cls, path, run_id):
        """Existing schema only. Never creates/upgrades a DB or acquires Run ownership."""
        def identify():
            db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                version = db.execute(
                    "SELECT version FROM state_schema WHERE singleton=1",
                ).fetchone()
                if version != (SCHEMA_VERSION,):
                    raise StateConflict("central_inspection_schema_unsupported")
                row = db.execute("SELECT engagement_id FROM state_runs WHERE run_id=?",
                                 (str(run_id),)).fetchone()
                if row is None:
                    raise StateConflict("central_original_run_missing")
                return row[0]
            finally:
                db.close()
        state = cls(path, run_id=str(run_id), engagement_id=await run_sqlite(identify))
        state.read_only = True
        return state

    async def create_mission(self, mission: MissionPlan) -> None:
        await self.transaction(lambda tx: tx.create_mission(mission))

    async def get_mission(self, mission_id: MissionId) -> MissionPlan | None:
        return await self.transaction(lambda tx: tx.get_mission(str(mission_id)), write=False)

    async def list_missions(
        self, *, engagement_id: EngagementId | None = None,
    ) -> list[MissionPlan]:
        if engagement_id is not None and str(engagement_id) != self.engagement_id:
            return []
        def read(tx: CentralTransaction) -> list[MissionPlan]:
            mission_id, _ = tx.binding()
            mission = tx.get_mission(mission_id)
            return [mission] if mission else []
        return await self.transaction(read, write=False)

    async def update_mission(self, mission: MissionPlan) -> None:
        await self.transaction(lambda tx: tx.update_mission(
            mission, expected_revision=mission.revision - 1,
        ))

    async def bind_run(
        self, mission_id: str, *, scope: EngagementScope, binding_hash: str,
    ) -> None:
        await self.transaction(lambda tx: tx.bind(mission_id, scope, binding_hash))

    async def register_task(
        self, request_id: str, task: MissionTask, *, checked: Sequence[CheckedEvidence] = (),
    ) -> MissionTask:
        return await self.transaction(lambda tx: tx.register_task(
            request_id, task, checked=checked,
        ))

    async def get_task(self, task_id: str) -> MissionTask | None:
        return await self.transaction(lambda tx: tx.get_task(task_id), write=False)

    async def list_tasks(
        self, *, mission_id: MissionId | None = None, status: TaskStatus | None = None,
    ) -> list[MissionTask]:
        def read(tx: CentralTransaction) -> list[MissionTask]:
            if mission_id is not None and str(mission_id) != tx.binding()[0]:
                return []
            tx.binding()
            query, args = "SELECT payload FROM state_tasks WHERE run_id=?", [self.run_id]
            if status is not None:
                query += " AND json_extract(payload,'$.status')=?"
                args.append(status.value)
            return [MissionTask.model_validate_json(row[0]).redacted_copy()
                    for row in tx.db.execute(query + " ORDER BY task_id", args)]
        return await self.transaction(read, write=False)

    async def get_execution(self, invocation_id: str) -> TaskExecution | None:
        return await self.transaction(lambda tx: tx.get_execution(invocation_id), write=False)

    async def has_inspected_evidence(self, evidence_id: str) -> bool:
        """A persisted reference denotes an inspected immutable original, not its body."""
        return await self.transaction(lambda tx: tx.db.execute(
            "SELECT 1 FROM state_evidence_refs WHERE run_id=? AND evidence_id=?",
            (self.run_id, evidence_id),
        ).fetchone() is not None, write=False)

    def exclusive_run(self, run_id: str):
        from contextlib import asynccontextmanager

        from redteam.orchestration.state_backend import run_storage_ownership

        @asynccontextmanager
        async def ownership():
            if run_id != self.run_id:
                raise StateConflict("central_run_identity_mismatch")
            with run_storage_ownership(self.path, run_id):
                yield
        return ownership()

    async def list_executions(self, *, task_id: str | None = None) -> list[TaskExecution]:
        def read(tx: CentralTransaction) -> list[TaskExecution]:
            tx.binding()
            query, args = "SELECT payload FROM state_executions WHERE run_id=?", [self.run_id]
            if task_id is not None:
                query += " AND task_id=?"
                args.append(task_id)
            return [TaskExecution.model_validate_json(row[0]) for row in
                    tx.db.execute(query + " ORDER BY invocation_id", args)]
        return await self.transaction(read, write=False)

    async def activation_inventory(self, *, instance_ids: set[str] | None = None) -> list[dict]:
        """Original proof rows for explicit quiescent recovery, not scheduling."""
        def read(tx):
            _, binding_hash = tx.binding()
            rows = tx.db.execute("SELECT instance_id,task_id,owner,ownership_version,launch_stage,"
                                 "proof_version,binding_hash FROM state_activations WHERE run_id=?",
                                 (tx.run_id,)).fetchall()
            names = ("instance_id", "task_id", "owner", "ownership_version", "launch_stage",
                     "proof_version", "binding_hash")
            inventory = [dict(zip(names, row, strict=True)) for row in rows]
            if instance_ids is not None:
                inventory = [row for row in inventory if row["instance_id"] in instance_ids]
            if any(row["proof_version"] != 1 or row["binding_hash"] != binding_hash
                   for row in inventory):
                raise StateConflict("activation_original_binding_mismatch")
            return inventory
        return await self.transaction(read, write=False)

    async def check_unentered(self, *, instance_id: str) -> MissionTask:
        def read(tx):
            tx.admit()
            _, binding = tx.binding()
            original = tx.db.execute("SELECT task_id,owner,ownership_version,launch_stage,"
                                     "proof_version,binding_hash FROM state_activations "
                                     "WHERE run_id=? AND instance_id=?",
                                     (tx.run_id, instance_id)).fetchone()
            if original is None or tuple(original[3:]) != ("reserved", 1, binding):
                raise StateConflict("never_entered_recovery_not_proven")
            task = tx._owned(original[0], original[1], original[2])
            tx._check_external_activity(original[0], any_activity=True)
            if (task.status is not TaskStatus.RUNNING
                    or tx.db.execute("SELECT 1 FROM state_executions WHERE run_id=? AND task_id=?",
                                     (tx.run_id, original[0])).fetchone() is not None
                    or tx._active_requests(original[1], any_activity=True)):
                raise StateConflict("never_entered_recovery_not_proven")
            return task
        return await self.transaction(read, write=False)

    async def claim_task(self, task_id: str, *, owner: str, instance_id: str) -> MissionTask:
        return await self.transaction(lambda tx: tx.claim_task(
            task_id, owner=owner, instance_id=instance_id,
        ))

    async def adjust_task_necessity(
        self, task_id: str, *, action: str, required=None, reason="", goal_revision=None,
        owner=None, version=None, criterion_refs=(), evidence_ids=(),
        source_task_id=None, source_result_id=None, source_question_id=None,
    ):
        def adjust(tx):
            return tx.adjust_task_necessity(
                task_id, action=action, required=required, reason=reason,
                goal_revision=goal_revision, owner=owner, version=version,
                criterion_refs=criterion_refs, evidence_ids=evidence_ids,
                source_task_id=source_task_id, source_result_id=source_result_id,
                source_question_id=source_question_id,
            )
        return await self.transaction(adjust)

    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None:
        return await self.transaction(lambda tx: tx.get_intent(str(intent_id)), write=False)

    async def append(self, record: IntentRecord) -> None:
        await self.transaction(lambda tx: tx.save_intent(record))

    async def update(self, record: IntentRecord) -> None:
        def update(tx: CentralTransaction) -> None:
            if tx.get_intent(str(record.intent_id)) is None:
                raise StateConflict("intent_not_received")
            tx.save_intent(record)
        await self.transaction(update)

    async def list_for_mission(
        self, mission_id: MissionId, *, status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]:
        def read(tx: CentralTransaction) -> list[IntentRecord]:
            if str(mission_id) != tx.binding()[0] or (limit is not None and limit <= 0):
                return []
            query, args = "SELECT payload FROM state_intents WHERE run_id=?", [self.run_id]
            if status is not None:
                query += " AND status=?"
                args.append(status.value)
            query += " ORDER BY created_at DESC,intent_id DESC"
            if limit is not None:
                query += " LIMIT ?"
                args.append(str(limit))
            records = [sanitize_intent_record(json.loads(row[0]))
                       for row in tx.db.execute(query, args)]
            return list(reversed(records))
        return await self.transaction(read, write=False)

    def request_ledger(self) -> RunRequestLedger:
        return RunRequestLedger(change=self._change_requests)

    def owns_request_ledger(self, ledger: RunRequestLedger) -> bool:
        return ledger.shares_boundary(self._change_requests)

    async def _change_requests(self, run_id: str, callback: RequestChange) -> Any:
        if run_id != self.run_id:
            raise CoordinationError("request_run_binding_mismatched")
        def change(tx: CentralTransaction) -> Any:
            tx.binding()
            records = _RequestRecords(tx)
            result = _finish_record_callback(callback(_RequestTransaction(tx, records)))
            records.flush()
            return result
        return await self.transaction(change)

    async def deliver_outbox(self, event_bus: Any, *, budget_seconds: float = 2.0) -> int:
        """Existing at-least-once dispatcher; publication happens after each commit."""
        return await deliver_pending(
            run_id=self.run_id, engagement_id=self.engagement_id, event_bus=event_bus,
            budget_seconds=budget_seconds, change=self._change_outbox,
        )

    async def _change_outbox(self, callback: OutboxChange) -> Any:
        def change(tx: CentralTransaction) -> Any:
            tx.binding()
            records = {("outbox", identity): json.loads(payload) for identity, payload
                       in tx.db.execute(
                           "SELECT event_id,payload FROM state_outbox WHERE run_id=? "
                           "AND json_extract(payload,'$.delivered')=0 ORDER BY sequence",
                           (self.run_id,),
                       )}
            previous = {key: json.dumps(value, sort_keys=True) for key, value in records.items()}
            records[("binding", self.run_id)] = {"engagement_id": self.engagement_id}
            result = _finish_record_callback(callback(SimpleNamespace(records=records)))
            for (kind, identity), payload in records.items():
                if kind == "outbox":
                    serialized = json.dumps(payload, sort_keys=True)
                    if serialized != previous[(kind, identity)]:
                        tx.db.execute(
                            "UPDATE state_outbox SET payload=? WHERE run_id=? AND event_id=?",
                            (serialized, self.run_id, identity),
                        )
            return result
        return await self.transaction(change)

    async def inspect_evidence(self, store: EvidenceStore) -> dict[str, list[str]]:
        """Read-only diagnostics, including external-only records after lost acknowledgement."""
        verifier = getattr(store, "verify_run_integrity", None)
        if not callable(verifier):
            raise CoordinationError("evidence_integrity_inspection_unavailable")
        bad_ids = list(map(str, await verifier(RunId(self.run_id))))
        def read(tx: CentralTransaction) -> dict[str, str]:
            tx.binding()
            return dict(tx.db.execute(
                "SELECT evidence_id,content_hash FROM state_evidence_refs WHERE run_id=?",
                (self.run_id,),
            ))
        refs = await self.transaction(read, write=False)
        missing, changed = [], []
        for identity, expected in refs.items():
            original = await store.get(EvidenceId(identity))
            if original is None:
                missing.append(identity)
            elif (str(original.run_id) != self.run_id
                  or str(original.engagement_id) != self.engagement_id
                  or content_hash(original) != expected):
                changed.append(identity)
        originals = await store.list_for_run(RunId(self.run_id))
        return {"file_index_disagreement": bad_ids, "missing": missing, "changed": changed,
                "external_only": sorted(str(item.evidence_id) for item in originals
                                        if str(item.evidence_id) not in refs)}
