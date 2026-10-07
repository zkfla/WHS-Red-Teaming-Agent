"""Registered result collection is run-bound and never invokes a gateway."""

from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.core.ids import AgentId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.job_local_tools import job_reconcile_handler
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.long_jobs import LongJobState, LongJobStatus
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus


def _work(identity, run):
    return WorkItem(
        work_id=identity, candidate_key=identity, run_id=RunId(run), agent_id=AgentId("worker"),
        target_node_ids=[], execution_origin_version=1, job_id="job-" + identity,
        priority=PriorityScore(total=1), status=WorkStatus.FAILED,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


async def test_list_registered_jobs_is_run_isolated_and_omits_reconciled():
    store = InMemoryWorkStore()
    await store.add(_work("ours", "run-one"))
    await store.add(_work("theirs", "run-two"))
    done = _work("done", "run-one").model_copy(update={"outcome_reason": "job_reconciled:failed"})
    await store.add(done)
    context = LocalToolContext(
        deps=SimpleNamespace(long_job_adapter=object(), work_store=store),
        runtime=SimpleNamespace(run_id=RunId("run-one")),
    )
    result = await job_reconcile_handler({}, context)
    assert result.ok
    assert [item["work_id"] for item in result.payload["jobs"]] == ["ours"]
    assert result.runtime_outcome is None


async def test_unregistered_or_foreign_work_never_reaches_job_backend():
    class Adapter:
        calls = 0

        async def status(self, job_id):
            self.calls += 1
            return LongJobStatus(job_id=job_id, state=LongJobState.RUNNING)

    store = InMemoryWorkStore()
    await store.add(_work("theirs", "run-two"))
    adapter = Adapter()
    context = LocalToolContext(
        deps=SimpleNamespace(long_job_adapter=adapter, work_store=store),
        runtime=SimpleNamespace(run_id=RunId("run-one")),
    )
    result = await job_reconcile_handler({"work_id": "theirs"}, context)
    assert not result.ok and result.payload["status"] == "no_registered_job"
    assert adapter.calls == 0


async def test_missing_adapter_is_an_explicit_failure():
    result = await job_reconcile_handler({}, LocalToolContext(
        deps=SimpleNamespace(long_job_adapter=None), runtime=SimpleNamespace(),
    ))
    assert not result.ok and "not supported" in result.error


async def test_already_collected_job_does_not_recheck_or_review():
    store = InMemoryWorkStore()
    done = _work("done", "run-one").model_copy(update={"outcome_reason": "job_reconciled:failed"})
    await store.add(done)
    result = await job_reconcile_handler({"work_id": "done"}, LocalToolContext(
        deps=SimpleNamespace(long_job_adapter=object(), work_store=store),
        runtime=SimpleNamespace(run_id=RunId("run-one")),
    ))
    assert result.ok and result.payload["status"] == "already_collected"


async def test_unknown_job_is_not_read_submitted_or_settled():
    class Adapter:
        async def status(self, job_id):
            return LongJobStatus(job_id=job_id, state=LongJobState.UNKNOWN)

        async def result(self, job_id):
            raise AssertionError("unknown results cannot be collected")

        async def submit(self, invocation):
            raise AssertionError("collection cannot submit an execution")

    store = InMemoryWorkStore()
    work = _work("unknown", "run-one")
    await store.add(work)
    context = LocalToolContext(
        deps=SimpleNamespace(
            long_job_adapter=Adapter(), work_store=store, llm_client=None,
            settings=OrchestrationSettings(), planning_provider=None,
        ),
        runtime=SimpleNamespace(run_id=RunId("run-one")),
    )
    result = await job_reconcile_handler({"work_id": work.work_id}, context)
    assert result.ok and result.payload["status"] == "unknown"
    assert result.runtime_outcome is None and not result.refs
    assert await store.get(work.work_id) == work


async def test_completed_job_without_preserved_action_stays_unresolved():
    class Adapter:
        async def status(self, job_id):
            return LongJobStatus(job_id=job_id, state=LongJobState.SUCCEEDED)

        async def result(self, job_id):
            raise AssertionError("a missing original cannot admit a backend result")

    class Evidence:
        async def list_for_run(self, run_id):
            return []

    store = InMemoryWorkStore()
    work = _work("missing", "run-one")
    await store.add(work)
    result = await job_reconcile_handler({"work_id": work.work_id}, LocalToolContext(
        deps=SimpleNamespace(
            long_job_adapter=Adapter(), work_store=store, evidence_store=Evidence(),
            llm_client=None, settings=OrchestrationSettings(), planning_provider=None,
        ),
        runtime=SimpleNamespace(run_id=RunId("run-one")),
    ))
    assert not result.ok and result.payload["status"] == "missing_original_action"
    assert await store.get(work.work_id) == work
