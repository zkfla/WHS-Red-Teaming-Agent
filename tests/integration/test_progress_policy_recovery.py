import pytest

from redteam_core.models.decision import ProgressPolicy
from redteam_core.runtime.step_loop import GenericStepEngine
from tests.integration.test_generic_step_loop import setup_engine


@pytest.mark.asyncio
async def test_explicit_same_policy_preserves_inflight_first_step(tmp_path):
    engine, session, runtime, candidate, driver, orchestrator = setup_engine(
        tmp_path, [], fail=True
    )
    try:
        await engine.run_step()
        before = runtime.progress.model_dump(mode="json")
        assert before["active_attempt_id"] is not None
        GenericStepEngine(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            driver=driver,
            active_candidate_ids=(candidate.candidate_id,),
            progress_policy=runtime.progress.policy,
            count_tokens=lambda text: len(text.encode("utf-8")) // 4,
        )
        assert runtime.progress.model_dump(mode="json") == before
    finally:
        orchestrator.close()


def test_rejected_policy_composition_does_not_mutate_runtime(tmp_path):
    _, session, runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    try:
        before = runtime.model_dump(mode="json")
        with pytest.raises(RuntimeError, match="composition mismatch"):
            GenericStepEngine(
                session=session,
                runtime=runtime,
                orchestrator=orchestrator,
                driver=driver,
                active_candidate_ids=(candidate.candidate_id,),
                progress_policy=ProgressPolicy(budget_drop_percent=25),
                count_tokens=lambda text: len(text.encode("utf-8")) // 4,
            )
        assert runtime.model_dump(mode="json") == before
    finally:
        orchestrator.close()
