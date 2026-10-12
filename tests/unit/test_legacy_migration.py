"""Offline fixture imports only: no user's originals, network or providers."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from redteam.app.legacy_migration import (
    MigrationRequest,
    migrate_legacy_storage,
    migrated_run_guard,
    validate_migrated_run,
)
from redteam.attack_graph.snapshot import to_records
from redteam.attack_graph.stores.mutations import GraphMutation, make_receipt
from redteam.control.audit import AuditLog
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.events import EventEnvelope
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.mission import InvestigationResult, OperatorIntent, ResultQuestion
from redteam.core.contracts.policy import Approval, PolicyDecision
from redteam.core.contracts.tool import ToolInvocation
from redteam.core.ids import OperatorIntentId
from redteam.core.tool_binding import execution_fingerprint
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.checkpointing import open_checkpointer
from redteam.orchestration.component_binding import BUILTIN_COMPONENT_IDS, RuntimeComponentBinding
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.execution_journal import append_execution_receipt
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.state_backend import run_storage_ownership
from redteam.orchestration.storage_snapshot import source_manifest
from redteam.orchestration.work.models import WorkItem
from redteam.sqlite_support import StorageRetiredError, retirement_path
from tests.unit.test_central_state import ENG, MISSION, NOW, RUN, mission, task
from tests.unit.test_checkpoint_threads import _write_thread
from tests.unit.test_network_ledger import budget, caps


async def fixture(tmp_path, *, entered=False, execution=False, network=False):
    source = tmp_path / "original"
    source.mkdir()
    refs = {name: source / name for name in ("evidence", "artifact", "audit", "approval")}
    for path in refs.values():
        path.mkdir()
    evidence = FilesystemEvidenceStore(refs["evidence"])
    approvals = FilesystemApprovalStore(refs["approval"])
    audit = FilesystemAuditStore(refs["audit"])
    engagement = Engagement.model_validate(
        {
            "engagement_id": ENG,
            "name": "Offline fixture",
            "scope": {},
            "execution_budget": {"max_actions": 10},
            "rate_limit_policy": {"max_actions_per_minute": 5},
            "approval_policy": {},
            "created_at": NOW,
        }
    )
    components = RuntimeComponentBinding(component_ids=BUILTIN_COMPONENT_IDS)
    policy = LLMRequestPolicy(mode="bounded", max_requests=8)
    inputs = {
        "schema_version": 2,
        "engagement": engagement.model_dump(mode="json", exclude={"created_at"}),
        "components": components.model_dump(mode="json"),
        "specialists": {"test": True},
    }
    current = type(task()).model_validate(
        {
            **task().model_dump(mode="json"),
            "status": "running",
            "occupant_owner": f"{RUN}:instance:instance-a",
            "ownership_version": 0,
            "completion_contract_version": 2,
            "completion_criteria": [{"kind": "evidence", "description": "Answer the question"}],
        }
    )
    runtime = RuntimeState(
        run_id=RUN,
        engagement_id=ENG,
        mission_id=MISSION,
        active_task_id=current.task_id,
        instance_id="instance-a",
        current_agent="web",
        started_at=NOW,
        remaining_budget=7,
        component_binding=components,
        llm_request_policy=policy.model_dump(mode="json"),
    )
    from redteam.core.contracts.completion import completion_contract_hash

    records = {
        ("binding", RUN): {"mission_id": MISSION, "engagement_id": ENG, "closed": False},
        ("planning_inputs", RUN): content_hash(inputs),
        ("llm_request_policy", RUN): policy.model_dump(mode="json"),
        ("task_activation_clock", RUN): NOW.isoformat(),
        ("task_activation", f"{RUN}:{current.task_id}"): {
            "run_id": RUN,
            "task_id": str(current.task_id),
            "instance_id": "instance-a",
            "state": "launched",
            "runtime": runtime.model_dump(mode="json"),
            "runtime_digest": content_hash(runtime),
            "role_id": "web",
            "launch_stage": "entered" if entered else "reserved",
            "launch_proof_version": 1,
            "completion_contract_hash": completion_contract_hash(current),
            "input_binding_hash": content_hash(inputs),
            "activation_generation": 0,
            "goal_input_hash": "a" * 64,
            "task_input_hash": "c" * 64,
        },
    }
    dbpath = source / "task.sqlite3"
    db = sqlite3.connect(dbpath)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.executescript("""
        CREATE TABLE missions (mission_id TEXT PRIMARY KEY, engagement_id TEXT, payload TEXT);
        CREATE TABLE mission_tasks (task_id TEXT PRIMARY KEY, mission_id TEXT, status TEXT,
                                   priority INTEGER, payload TEXT);
        CREATE TABLE orchestration_migrations (version INTEGER PRIMARY KEY);
        CREATE TABLE orchestration_records (kind TEXT, identity TEXT, payload TEXT,
                                            PRIMARY KEY(kind,identity));
        CREATE TABLE path_progress (step_id TEXT PRIMARY KEY, state TEXT);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT);
    """)
    db.execute("INSERT INTO orchestration_migrations VALUES(1)")
    db.execute("INSERT INTO missions VALUES(?,?,?)", (MISSION, ENG, mission().model_dump_json()))
    db.execute(
        "INSERT INTO mission_tasks VALUES(?,?,?,?,?)",
        (
            current.task_id,
            MISSION,
            current.status.value,
            current.priority,
            current.model_dump_json(),
        ),
    )
    db.executemany(
        "INSERT INTO orchestration_records VALUES(?,?,?)",
        [(kind, key, json.dumps(value)) for (kind, key), value in records.items()],
    )
    db.execute("INSERT INTO path_progress VALUES('past-step','pending')")
    db.execute("INSERT INTO tasks VALUES('past-task','ready')")
    db.commit()  # Keep WAL open: the migration must copy its committed records.
    from redteam.orchestration.coordination import ChangeSet
    from redteam.orchestration.paths.memory import InMemoryPathStore
    from redteam.orchestration.planning_admission import goal_input_hash, task_input_hash
    from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

    tx = ChangeSet(SQLiteTaskGraphStore._load_memory(db), InMemoryPathStore(), records)
    activation = records[("task_activation", f"{RUN}:{current.task_id}")]
    activation.update(
        goal_input_hash=goal_input_hash(tx, MISSION),
        task_input_hash=await task_input_hash(tx, str(current.task_id)),
    )
    db.execute(
        "UPDATE orchestration_records SET payload=? WHERE kind='task_activation'",
        (json.dumps(activation),),
    )
    db.commit()
    databases = {"task": dbpath}
    if entered:
        checkpoint = source / "checkpoint.sqlite3"
        async with open_checkpointer(backend="sqlite", path=str(checkpoint)) as saver:
            await _write_thread(
                saver, f"{RUN}:instance:instance-a", runtime.model_dump(mode="json")
            )
        databases["checkpoint"] = checkpoint
    if execution:
        proposal = ActionProposal.model_validate(
            {
                "action_id": "action-original",
                "run_id": RUN,
                "engagement_id": ENG,
                "agent_id": "web",
                "instance_id": "instance-a",
                "binding_mode": "selected_tool",
                "tool_id": "tool-fixture",
                "target": None,
                "arguments": {},
                "reason": "Offline fixture",
                "created_at": NOW,
            }
        )
        invocation = ToolInvocation.model_validate(
            {
                "invocation_id": "invoke-original",
                "action_id": proposal.action_id,
                "run_id": RUN,
                "tool_id": proposal.tool_id,
                "binding_mode": "selected_tool",
                "arguments": {},
                "created_at": NOW,
                "execution_context": {
                    "engagement_id": ENG,
                    "run_id": RUN,
                    "agent_id": "web",
                    "instance_id": "instance-a",
                },
            }
        )
        step = PlanStep.model_validate(
            {
                "step_id": "step-fixture",
                "tool_id": proposal.tool_id,
                "binding_mode": "selected_tool",
                "execution_origin_version": 1,
                "mission_task_id": current.task_id,
                "task_purpose_hash": completion_contract_hash(current),
                "model_tool_call_id": "call-original",
                "reason": "Offline fixture",
            }
        )
        item = WorkItem.model_validate(
            {
                "work_id": "work-original",
                "run_id": RUN,
                "candidate_key": step.candidate_key,
                "agent_id": "web",
                "instance_id": "instance-a",
                "mission_task_id": current.task_id,
                "model_tool_call_id": "call-original",
                "task_purpose_hash": completion_contract_hash(current),
                "execution_origin_version": 1,
                "target_node_ids": [],
                "priority": {"total": 0},
                "created_at": NOW,
                "updated_at": NOW,
                "status": "cancelled",
                "attempt": 1,
                "job_id": "job-original",
                "conflict_keys": ["session-original"],
                "resource_keys": ["session-original"],
                "resource_execution_pending": True,
            }
        )
        await append_execution_receipt(
            evidence, invocation=invocation, proposal=proposal, step=step, work_id=item.work_id
        )
        fingerprint = execution_fingerprint(proposal.tool_id, proposal.target, proposal.arguments)
        approval = Approval.model_validate(
            {
                "approval_id": "approval-original",
                "action_id": proposal.action_id,
                "status": "approved",
                "requested_at": NOW,
                "resolved_at": NOW,
                "execution_fingerprint": fingerprint,
            }
        )
        await approvals.save(approval)
        await AuditLog(audit).record(
            proposal,
            PolicyDecision.model_validate(
                {
                    "action_id": proposal.action_id,
                    "verdict": "require_approval",
                    "approval_id": approval.approval_id,
                    "evaluated_at": NOW,
                    "execution_fingerprint": fingerprint,
                }
            ),
        )
        workpath = source / "work.sqlite3"
        with sqlite3.connect(workpath) as workdb:
            workdb.execute(
                "CREATE TABLE work_items (work_id TEXT PRIMARY KEY, run_id TEXT, "
                "instance_id TEXT, candidate_key TEXT, status TEXT, priority REAL, payload TEXT)"
            )
            workdb.execute(
                "INSERT INTO work_items VALUES(?,?,?,?,?,?,?)",
                (
                    item.work_id,
                    RUN,
                    "instance-a",
                    item.candidate_key,
                    item.status.value,
                    0,
                    item.model_dump_json(),
                ),
            )
        databases["work"] = workpath
        records[("llm_request", "request-original")] = {
            "request_id": "request-original",
            "run_id": RUN,
            "owner": runtime.work_owner,
            "kind": "invoke",
            "state": "unknown",
            "total_tokens": None,
        }
        db.execute(
            "INSERT INTO orchestration_records VALUES(?,?,?)",
            (
                "llm_request",
                "request-original",
                json.dumps(records[("llm_request", "request-original")]),
            ),
        )
        db.commit()
    if network:
        networkpath = source / "network.sqlite3"
        ledger = SQLiteNetworkGrantLedger(
            networkpath, engagement_id=ENG, budget=budget(), now=lambda: 100
        )
        await ledger.setup()
        window = await ledger.request_window(
            run_id=RUN, instance_id="instance-a", epoch=1, caps=caps()
        )
        await ledger.admit_delivery(
            window_id=window.window_id,
            daemon_session_id="daemon-original",
            seq=1,
            flow_key="flow-original",
        )
        await ledger.settle_window(window_id=window.window_id, meter=None, verified_closed=False)
        databases["network"] = networkpath
    request = MigrationRequest(
        "migration-fixture",
        tmp_path / "target.sqlite3",
        databases,
        refs,
        {RUN: inputs},
        offline=True,
    )
    state = CentralStateStore(request.target, run_id=RUN, engagement_id=ENG)
    validation = {
        "current_inputs": inputs,
        "current_policy": policy,
        "component_check": lambda b: None,
        "evidence_store": evidence,
        "approval_store": approvals,
        "audit_store": audit,
    }
    return request, state, evidence, approvals, db, current, validation


async def test_wal_originals_idempotence_and_original_ownership(tmp_path):
    request, state, evidence, approvals, original, _, validation = await fixture(tmp_path)
    before = source_manifest(dict(request.databases), dict(request.references))
    try:
        report = await migrate_legacy_storage(request, evidence_store=evidence)
        assert report["runs"][RUN]["eligible_for_resume_validation"]
        assert report == await migrate_legacy_storage(request, evidence_store=evidence)
        assert source_manifest(dict(request.databases), dict(request.references)) == before
        with pytest.raises(CoordinationError, match="migration_resume_guard_required"):
            await state.transaction(lambda tx: tx.admit())
        async with migrated_run_guard(state, **validation):
            assert await state.transaction(lambda tx: tx.admit())
            reservation = await state.request_ledger().reserve(RUN, owner="planner", kind="fixture")
            await state.request_ledger().dispatch(RUN, reservation)
            with pytest.raises(CoordinationError, match="already_owned"):
                with run_storage_ownership(
                    request.databases["task"], RUN, migrated_target=state.path
                ):
                    pass
        with pytest.raises(CoordinationError, match="migration_resume_guard_required"):
            await state.transaction(lambda tx: tx.admit())
        with pytest.raises(StorageRetiredError, match="storage_retired"):
            with run_storage_ownership(request.databases["task"], "another-original-run"):
                pass
        second = tmp_path / "another-target.sqlite3"
        with pytest.raises(StateConflict, match="retired_elsewhere"):
            await migrate_legacy_storage(replace(request, target=second), evidence_store=evidence)
        assert not second.exists()
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 1
            assert db.execute("SELECT ownership_version FROM state_activations").fetchone()[0] == 1
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM state_migration_records WHERE "
                    "reason='historical_path_not_a_prerequisite'"
                ).fetchone()[0]
                == 1
            )
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='path_progress'"
            ).fetchone()
            # Mapping keys contain only original IDs, not another copy of bodies.
            assert all(
                "payload" not in row[0]
                for row in db.execute(
                    "SELECT target_key FROM state_migration_records WHERE target_key IS NOT NULL"
                )
            )
        assert (await state.request_ledger().snapshot(RUN))["reserved"] == 1
    finally:
        original.close()


async def test_unknown_execution_llm_and_network_never_refunded(tmp_path):
    request, state, evidence, approvals, original, _, validation = await fixture(
        tmp_path, entered=True, execution=True, network=True
    )
    try:
        report = await migrate_legacy_storage(
            request,
            evidence_store=evidence,
            approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        assert report["runs"][RUN]["eligible_for_resume_validation"], report
        execution = await state.transaction(
            lambda tx: tx.get_execution("invoke-original"), write=False
        )
        assert execution.state is ExecutionState.UNKNOWN
        assert execution.instance_id == "instance-a" and execution.job_ref == "job-original"
        assert execution.approval_ref == "approval-original"
        assert execution.resource_execution_pending
        with (
            sqlite3.connect(state.path) as db,
            sqlite3.connect(
                Path(request.databases["network"]).as_uri() + "?immutable=1",
                uri=True,
            ) as old,
        ):
            for table in ("network_pools", "network_windows", "network_seq_log"):
                assert (
                    db.execute(f"SELECT * FROM {table}").fetchall()
                    == old.execute(f"SELECT * FROM {table}").fetchall()
                )
            assert db.execute("SELECT resource_key FROM state_execution_resources").fetchall() == [
                ("session-original",)
            ]
        snapshot = await state.request_ledger().snapshot(RUN)
        assert snapshot["spent"] == 1 and snapshot["unknown_usage"] == 1
        identity = await validate_migrated_run(state, **validation)
        assert identity["identity"]["checkpoints"][0]["remaining_budget"] == 7
        assert report == await migrate_legacy_storage(
            request,
            evidence_store=evidence,
            approval_store=approvals,
            audit_store=validation["audit_store"],
        )
    finally:
        original.close()


async def test_active_or_unstopped_sources_refused(tmp_path):
    request, state, evidence, _, original, _, _ = await fixture(tmp_path)
    try:
        with pytest.raises(StateConflict, match="stopped_offline"):
            await migrate_legacy_storage(replace(request, offline=False))
        with run_storage_ownership(request.databases["task"], RUN):
            with pytest.raises(CoordinationError, match="already_owned"):
                await migrate_legacy_storage(request, evidence_store=evidence)
        assert not state.path.exists()
    finally:
        original.close()


async def test_interrupted_import_rolls_back_and_retries_once(tmp_path, monkeypatch):
    request, state, evidence, _, original, _, _ = await fixture(tmp_path)
    from redteam.orchestration import legacy_migration

    real = legacy_migration._write_import

    def interrupt(*args):
        real(*args)
        raise RuntimeError("fixture interrupted before commit")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(legacy_migration, "_write_import", interrupt)
            with pytest.raises(RuntimeError, match="interrupted"):
                await migrate_legacy_storage(request, evidence_store=evidence)
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM state_migrations").fetchone()[0] == 0
        await migrate_legacy_storage(request, evidence_store=evidence)
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 1
    finally:
        original.close()


async def test_changed_binding_pending_writer_and_unsupported_checkpoint_refused(tmp_path):
    request, state, evidence, _, original, _, validation = await fixture(tmp_path, entered=True)
    try:
        original.execute(
            "INSERT INTO orchestration_records VALUES(?,?,?)",
            ("planning_reference", f"{RUN}:graph", json.dumps({"version": 1, "writer": "pending"})),
        )
        original.commit()
        with sqlite3.connect(request.databases["checkpoint"]) as db:
            db.execute("UPDATE checkpoints SET type='unsupported-future-codec'")
        report = await migrate_legacy_storage(request, evidence_store=evidence)
        reasons = report["runs"][RUN]["reasons"]
        assert "checkpoint_format_or_identity_unverified" in reasons
        assert "pending_planning_write_requires_original_confirmation" in reasons
        with pytest.raises(StateConflict, match="resume_refused"):
            await validate_migrated_run(state, **validation)
        with pytest.raises(CoordinationError, match="resume_refused"):
            await state.transaction(lambda tx: tx.admit())
        with pytest.raises(StateConflict, match="identity_source_conflict"):
            await migrate_legacy_storage(
                replace(request, inputs_by_run={RUN: {}}), evidence_store=evidence
            )
    finally:
        original.close()


async def test_results_questions_lineage_graph_receipt_and_intent_not_reapplied(tmp_path):
    request, state, evidence, _, original, current, validation = await fixture(tmp_path)
    try:
        result = InvestigationResult(
            result_id="result-original",
            outcome="refuted",
            summary="Original negative answer",
            questions=[
                ResultQuestion(
                    question_id="question-original",
                    question="Investigate another route",
                    required_for_goal=True,
                    reason="Original follow-up",
                )
            ],
        )
        answered = type(current).model_validate(
            {
                **task("answered").model_dump(mode="json"),
                "status": "completed",
                "completion_contract_version": 2,
                "completion_criteria": [{"kind": "evidence", "description": "Original question"}],
                "result": result.model_dump(mode="json"),
            }
        )
        followup = type(current).model_validate(
            {
                **task("followup").model_dump(mode="json"),
                "source_task_id": "answered",
                "source_result_id": result.result_id,
                "source_question_id": "question-original",
                "priority": 99,
                "parent_task_id": "answered",
                "dependency_task_ids": ["answered"],
            }
        )
        for item in (answered, followup):
            original.execute(
                "INSERT INTO mission_tasks VALUES(?,?,?,?,?)",
                (item.task_id, MISSION, item.status.value, item.priority, item.model_dump_json()),
            )
        intent = IntentRecord.model_validate(
            {
                "intent": OperatorIntent.model_validate(
                    {
                        "intent_id": "intent-original",
                        "mission_id": MISSION,
                        "intent_type": "reprioritize",
                        "instruction": "Investigate this",
                        "created_at": NOW,
                        "provenance": {"source": "test"},
                    }
                ),
                "status": "applied",
                "run_id": RUN,
                "correlation_id": "correlation-original",
                "outcome": "already applied",
                "applied_at": NOW,
                "applied_event_id": "event-original",
            }
        )
        event = EventEnvelope.model_validate(
            {
                "event_id": "event-original",
                "event_type": "operator_intent_applied",
                "run_id": RUN,
                "engagement_id": ENG,
                "source": "fixture",
                "timestamp": NOW,
                "payload": {"intent_id": "intent-original"},
            }
        )
        original.execute(
            "INSERT INTO orchestration_records VALUES(?,?,?)",
            ("intent", "intent-original", intent.model_dump_json()),
        )
        original.execute(
            "INSERT INTO orchestration_records VALUES(?,?,?)",
            (
                "outbox",
                "event-original",
                json.dumps(
                    {
                        "sequence": 3,
                        "envelope": event.model_dump(mode="json"),
                        "delivered": True,
                        "owner": None,
                        "lease_until": 0,
                    }
                ),
            ),
        )
        original.execute(
            "INSERT INTO orchestration_records VALUES('outbox_sequence',?,?)", (RUN, json.dumps(9))
        )
        original.commit()
        node = GraphNode(
            id=f"{ENG}\x1fhost:original",
            type="host",
            created_at=NOW,
            updated_at=NOW,
            properties={
                "sources": ["source-original"],
                "retracted": True,
                "claim_provenance": {"facts:observed": {"run_id": RUN}},
            },
        )
        graphpath = Path(request.databases["task"]).parent / "graph.sqlite3"
        mutation = GraphMutation(
            run_id=RUN,
            mutation_id="mutation-original",
            namespace=ENG,
            operation="add_node",
            node=node,
            references=("graph",),
        )
        receipt = make_receipt(mutation, None)
        legacy_edge = GraphEdge(
            id="related_to:original",
            type="related_to",
            source_id=node.id,
            target_id=node.id,
            properties={"source": "original"},
            created_at=NOW,
        )
        with sqlite3.connect(graphpath) as db:
            db.execute(
                "CREATE TABLE graph_records (collection TEXT, record_id TEXT, source_id TEXT, "
                "target_id TEXT, record TEXT, PRIMARY KEY(collection,record_id))"
            )
            for row in to_records([node], [legacy_edge]):
                db.execute(
                    "INSERT INTO graph_records VALUES(?,?,?,?,?)",
                    (
                        row["_collection"],
                        row.get("id", ""),
                        row.get("source_id"),
                        row.get("target_id"),
                        json.dumps(row),
                    ),
                )
            db.execute(
                "CREATE TABLE graph_mutation_receipts (namespace TEXT, run_id TEXT, "
                "mutation_id TEXT, receipt TEXT, PRIMARY KEY(namespace,run_id,mutation_id))"
            )
            db.execute(
                "INSERT INTO graph_mutation_receipts VALUES(?,?,?,?)",
                (ENG, RUN, receipt.mutation_id, receipt.model_dump_json()),
            )
        request = replace(request, databases={**request.databases, "graph": graphpath})
        report = await migrate_legacy_storage(request, evidence_store=evidence)
        assert report["runs"][RUN]["eligible_for_resume_validation"], report
        restored = await state.get_task("answered")
        assert restored.result == result and restored.completion_contract_version == 2
        restored = await state.get_task("followup")
        assert restored.source_question_id == "question-original"
        assert restored.priority == 0 and restored.dependency_task_ids == []
        assert (await state.get(OperatorIntentId("intent-original"))).status.value == "applied"
        async with migrated_run_guard(state, **validation):
            applied = []
            await state.transaction(
                lambda tx: tx.apply_intent(
                    intent, mutate=lambda tx: applied.append("incorrect second effect")
                )
            )
            assert not applied
            await state.transaction(
                lambda tx: tx.queue(
                    event.model_copy(
                        update={"event_id": "new-event"},
                    )
                )
            )
        restored_node = await state.transaction(lambda tx: tx.graph.get_node(node.id), write=False)
        assert restored_node == node
        restored_edge = await state.transaction(
            lambda tx: tx.graph.get_edge(f"{ENG}\x1f{legacy_edge.id}"),
            write=False,
        )
        assert restored_edge == legacy_edge.model_copy(update={"id": f"{ENG}\x1f{legacy_edge.id}"})
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT receipt FROM state_graph_mutations").fetchone()[0] == (
                receipt.model_dump_json()
            )
            assert json.loads(db.execute("SELECT payload FROM state_outbox").fetchone()[0])[
                "delivered"
            ]
            assert (
                db.execute(
                    "SELECT sequence FROM state_outbox WHERE event_id='new-event'"
                ).fetchone()[0]
                == 10
            )
        assert report == await migrate_legacy_storage(request, evidence_store=evidence)
    finally:
        original.close()


async def test_missing_input_binding_and_malformed_rows_retained_and_not_resumable(tmp_path):
    request, state, evidence, _, original, _, validation = await fixture(tmp_path)
    try:
        original.execute("UPDATE mission_tasks SET payload='not-json' WHERE task_id=?", ("task-a",))
        original.commit()
        report = await migrate_legacy_storage(
            replace(request, inputs_by_run={}), evidence_store=evidence
        )
        assert not report["runs"][RUN]["eligible_for_resume_validation"]
        assert report["reasons"]["unreadable_task"] == 1
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 0
        with pytest.raises(StateConflict, match="resume_refused"):
            await validate_migrated_run(state, **validation)
    finally:
        original.close()


async def test_pre_submission_approval_retained_with_checkpoint_and_resume_refused(tmp_path):
    request, state, evidence, approvals, original, _, validation = await fixture(
        tmp_path, entered=True
    )
    try:
        runtime = RuntimeState.model_validate(
            json.loads(
                original.execute(
                    "SELECT payload FROM orchestration_records WHERE kind='task_activation'",
                ).fetchone()[0]
            )["runtime"]
        )
        runtime.pending_action_id = "action-pending"
        runtime.pending_approval_id = "approval-pending"
        approval = Approval.model_validate(
            {
                "approval_id": "approval-pending",
                "action_id": "action-pending",
                "status": "pending",
                "execution_fingerprint": "f" * 64,
                "requested_at": NOW,
            }
        )
        await approvals.save(approval)
        async with open_checkpointer(
            backend="sqlite", path=str(request.databases["checkpoint"])
        ) as saver:
            await _write_thread(
                saver, f"{RUN}:instance:instance-a", runtime.model_dump(mode="json")
            )
        report = await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals
        )
        assert "checkpoint_approval_requires_runtime_adapter" in report["runs"][RUN]["reasons"]
        with sqlite3.connect(state.path) as db:
            identity = json.loads(
                db.execute("SELECT identity FROM state_migrated_runs").fetchone()[0]
            )
            assert identity["checkpoints"][0]["pending_approval_id"] == "approval-pending"
            assert identity["checkpoints"][0]["pending_action_id"] == "action-pending"
        assert await approvals.get("approval-pending") == approval
        with pytest.raises(StateConflict, match="resume_refused"):
            await validate_migrated_run(state, **validation)
    finally:
        original.close()


async def test_committed_import_without_destination_fence_cannot_resume_then_retries(
    tmp_path, monkeypatch
):
    request, state, evidence, _, original, _, validation = await fixture(tmp_path)
    from redteam.orchestration import legacy_migration

    try:
        with monkeypatch.context() as patch:

            def interrupt(*args):
                raise RuntimeError("fixture interrupted after commit")

            patch.setattr(legacy_migration, "retire_storage", interrupt)
            with pytest.raises(RuntimeError, match="after commit"):
                await migrate_legacy_storage(request, evidence_store=evidence)
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 1
        with pytest.raises(StateConflict, match="destination_fence_missing"):
            await validate_migrated_run(state, **validation)
        await migrate_legacy_storage(request, evidence_store=evidence)
        assert retirement_path(request.databases["task"]).exists()
        assert await validate_migrated_run(state, **validation)
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 1
    finally:
        original.close()


async def test_historical_execution_does_not_invent_current_goal_revision(tmp_path):
    request, state, evidence, approvals, original, _, validation = await fixture(
        tmp_path,
        entered=True,
        execution=True,
    )
    try:
        revised = mission().model_copy(update={"revision": 2})
        original.execute("UPDATE missions SET payload=?", (revised.model_dump_json(),))
        original.commit()
        report = await migrate_legacy_storage(
            request,
            evidence_store=evidence,
            approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        assert "original_execution_goal_revision_unverified" in report["runs"][RUN]["reasons"]
        execution = await state.transaction(
            lambda tx: tx.get_execution("invoke-original"), write=False
        )
        assert execution.goal_revision is None
        assert execution.approval_ref == "approval-original"
        assert execution.resource_execution_pending
        with pytest.raises(StateConflict, match="resume_refused"):
            await validate_migrated_run(state, **validation)
    finally:
        original.close()
