"""Plan 4d unit coverage (F05/F17). Stub doubles only: no live MCP, target, or LLM."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.app.cli import _build_parser
from redteam.app.cli.commands import resume as resume_command
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.job_local_tools import job_reconcile_handler
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.settings import OrchestrationSettings


def test_collect_originals_registered_through_dispatcher():
    args = _build_parser().parse_args(["collect-originals", "run_1", "eng.yaml"])
    assert args.handler is resume_command.run_collect_originals
    assert args.run_id == "run_1"
    assert args.engagement_path == "eng.yaml"


def test_collect_originals_has_no_legacy_runtime_flag():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["collect-originals", "run_1", "eng.yaml", "--runtime", "legacy"],
        )


def test_resume_still_registered():
    args = _build_parser().parse_args(["resume", "run_1", "eng.yaml"])
    assert args.handler is resume_command.run


_DIGEST = "ab" * 32


def _execution(**overrides):
    values = {
        "engagement_id": "eng-one",
        "run_id": "run-one",
        "task_id": "task-1",
        "instance_id": "a",
        "ownership_version": 1,
        "action_id": "act-1",
        "invocation_id": "inv-1",
        "model_tool_call_id": "call-1",
        "tool_id": "web.inspect",
        "input_digest": _DIGEST,
        "proposal_evidence_id": "ev-1",
        "state": ExecutionState.UNKNOWN,
        "wait_reason": "job",
        "job_ref": "job-1",
    }
    values.update(overrides)
    return TaskExecution.model_validate(values)


def _central_context(executions, *, adapter=None, instance_id="a", task_id="task-1"):
    async def list_executions(*, task_id=None):
        if task_id is None:
            return list(executions)
        return [item for item in executions if str(item.task_id) == task_id]

    async def get_execution(invocation_id):
        for item in executions:
            if str(item.invocation_id) == str(invocation_id):
                return item
        return None

    async def get_task(task_id):
        return SimpleNamespace(task_id=task_id)

    state = SimpleNamespace(
        run_id="run-one",
        engagement_id="eng-one",
        list_executions=list_executions,
        get_execution=get_execution,
        get_task=get_task,
    )
    deps = SimpleNamespace(central_state=state, long_job_adapter=adapter)
    runtime = SimpleNamespace(run_id="run-one", instance_id=instance_id, active_task_id=task_id)
    return LocalToolContext(deps=deps, runtime=runtime)


async def test_central_list_returns_only_masked_current_instance_jobs():
    executions = [
        _execution(),
        _execution(invocation_id="inv-2", job_ref="job-2", instance_id="b"),
        _execution(invocation_id="inv-3", job_ref=None, wait_reason=None),
    ]
    context = _central_context(executions, adapter=object())
    result = await job_reconcile_handler({}, context)
    assert result.ok
    assert result.payload["long_job_adapter"] == "bound"
    (row,) = result.payload["jobs"]
    assert row == {
        "job_ref": "job-1",
        "invocation_id": "inv-1",
        "action_id": "act-1",
        "task_id": "task-1",
        "instance_id": "a",
        "state": "unknown",
        "wait_reason": "job",
    }


async def test_central_list_without_adapter_reports_availability():
    context = _central_context([_execution()], adapter=None)
    result = await job_reconcile_handler({}, context)
    assert result.ok and len(result.payload["jobs"]) == 1
    assert result.payload["long_job_adapter"].startswith("unavailable")


async def test_central_work_id_is_an_explicit_compatibility_error():
    context = _central_context([_execution()], adapter=object())
    result = await job_reconcile_handler({"work_id": "w-1"}, context)
    assert not result.ok
    assert result.payload["status"] == "work_id_not_supported_centrally"


async def test_central_missing_job_is_rejected_without_backend_touch():
    class Adapter:
        async def status(self, job_id):
            raise AssertionError("missing jobs never reach the backend")

    context = _central_context([], adapter=Adapter())
    assert (await job_reconcile_handler({"invocation_id": "inv-x"}, context)).payload == {
        "status": "no_registered_job",
    }
    assert (await job_reconcile_handler({"job_ref": "job-x"}, context)).payload == {
        "status": "no_registered_job",
    }


async def test_central_ambiguous_job_ref_is_rejected():
    executions = [
        _execution(),
        _execution(invocation_id="inv-2", action_id="act-2"),
    ]
    context = _central_context(executions, adapter=object())
    result = await job_reconcile_handler({"job_ref": "job-1"}, context)
    assert not result.ok and result.payload["status"] == "ambiguous_job_ref"


async def test_central_foreign_run_instance_task_refs_are_rejected():
    foreign = _execution(run_id="run-two")
    context = _central_context([foreign], adapter=object())
    result = await job_reconcile_handler({"invocation_id": "inv-1"}, context)
    assert not result.ok and result.payload["status"] == "no_registered_job"
    sibling = _execution(instance_id="b")
    context = _central_context([sibling], adapter=object())
    by_job = await job_reconcile_handler({"job_ref": "job-1"}, context)
    assert not by_job.ok and by_job.payload["status"] == "wrong_owner_ref"
    other_task = _execution(task_id="task-9")
    context = _central_context([other_task], adapter=object())
    by_invocation = await job_reconcile_handler({"invocation_id": "inv-1"}, context)
    assert not by_invocation.ok and by_invocation.payload["status"] == "wrong_task_ref"


async def test_central_execution_without_job_ref_is_rejected():
    executions = [_execution(job_ref=None, wait_reason=None, state=ExecutionState.RUNNING)]
    context = _central_context(executions, adapter=object())
    result = await job_reconcile_handler({"invocation_id": "inv-1"}, context)
    assert not result.ok and result.payload["status"] == "no_registered_job"


async def test_central_already_collected_needs_no_backend():
    class Adapter:
        async def status(self, job_id):
            raise AssertionError("collected jobs never reach the backend")

        async def result(self, job_id):
            raise AssertionError("collected jobs never reach the backend")

    executions = [_execution(state=ExecutionState.SUCCEEDED, result_evidence_id="ev-r")]
    context = _central_context(executions, adapter=Adapter())
    result = await job_reconcile_handler({"invocation_id": "inv-1"}, context)
    assert result.ok and result.payload["status"] == "already_collected"
    assert result.payload["result_evidence_id"] == "ev-r"


class _RunOwnership:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


def _proof(instance_id, task_id):
    return {
        "instance_id": instance_id,
        "task_id": task_id,
        "owner": "owner-" + instance_id,
        "ownership_version": 1,
        "launch_stage": "entered",
        "proof_version": 1,
        "binding_hash": "hash-1",
    }


def _driver_deps(executions, record, proofs=None):
    if proofs is None:
        proofs = [_proof("a", "task-1")]

    async def activation_inventory(*, instance_ids=None):
        rows = list(proofs)
        if instance_ids is not None:
            rows = [row for row in rows if row["instance_id"] in instance_ids]
        return rows

    async def get_task(task_id):
        return SimpleNamespace(task_id=task_id)

    async def list_executions(*, task_id=None):
        return [item for item in executions if task_id is None or str(item.task_id) == task_id]

    async def verify_run_integrity(run_id):
        return set()

    async def evidence_get(evidence_id):
        reads.append(str(evidence_id))
        return record

    state = SimpleNamespace(
        run_id="run-one",
        engagement_id="eng-one",
        activation_inventory=activation_inventory,
        get_task=get_task,
        list_executions=list_executions,
        exclusive_run=lambda run_id: _RunOwnership(),
    )
    reads = []
    evidence = SimpleNamespace(
        verify_run_integrity=verify_run_integrity, get=evidence_get, reads=reads,
    )
    deps = SimpleNamespace(
        central_state=state,
        evidence_store=evidence,
        execution_recovery=None,
        long_job_adapter=None,
    )
    settings = OrchestrationSettings(checkpointing=False)
    return deps, settings


def _bare_record():
    return EvidenceRecord(
        evidence_id="ev-1",
        run_id="run-one",
        engagement_id="eng-one",
        source_type=EvidenceSourceType.AGENT,
        source_id="orchestration.execute",
        content_type="application/json",
        digest="digest-1",
        created_at=datetime.now(timezone.utc),
    )


async def test_checkpointless_missing_original_stays_held():
    deps, settings = _driver_deps([_execution()], None)
    async with InstanceDriver(deps, settings=settings) as driver:
        (collection,) = await driver.collect_checkpointless_originals(
            "run-one", checkpoint_instances=set(),
        )
    (original,) = collection.originals
    assert original.invocation_id == "inv-1"
    assert original.state == "held"
    assert original.reason_code == "original_evidence_missing"
    assert original.original_receipt_id is None


async def test_checkpointless_scope_mismatch_is_rejected():
    record = _bare_record().model_copy(update={"run_id": "run-other"})
    deps, settings = _driver_deps([_execution()], record)
    async with InstanceDriver(deps, settings=settings) as driver:
        (collection,) = await driver.collect_checkpointless_originals(
            "run-one", checkpoint_instances=set(),
        )
    (original,) = collection.originals
    assert original.state == "invalid"
    assert original.reason_code == "evidence_scope_mismatch"


async def test_checkpointed_instances_are_skipped():
    deps, settings = _driver_deps([_execution()], None)
    async with InstanceDriver(deps, settings=settings) as driver:
        assert await driver.collect_checkpointless_originals(
            "run-one", checkpoint_instances={"a"},
        ) == []


async def test_checkpointless_instance_selection_leaves_siblings_untouched():
    proofs = [_proof("a", "task-1"), _proof("b", "task-2")]
    executions = [
        _execution(),
        _execution(
            task_id="task-2", instance_id="b", invocation_id="inv-2",
            action_id="act-2", proposal_evidence_id="ev-2", job_ref="job-2",
        ),
    ]
    deps, settings = _driver_deps(executions, None, proofs)
    async with InstanceDriver(deps, settings=settings) as driver:
        collections = await driver.collect_checkpointless_originals(
            "run-one", checkpoint_instances=set(), instance_ids={"a"},
        )
    assert [item.instance_id for item in collections] == ["a"]
    assert deps.evidence_store.reads == ["ev-1"]
