import pytest

from redteam_core.models import DecisionStepOutcome
from redteam_core.runtime.step_loop import semantic_progress_fingerprint
from tests.integration.test_generic_step_loop import setup_engine


@pytest.mark.parametrize(
    "outcome",
    [DecisionStepOutcome.REJECTED, DecisionStepOutcome.ERROR, DecisionStepOutcome.NO_PROGRESS],
)
def test_repeated_outcomes_cannot_escape_via_context_revision_churn(tmp_path, outcome):
    engine, session, runtime, _, _, manager = setup_engine(tmp_path, [])
    before = semantic_progress_fingerprint(session)
    try:
        actual = []
        for index in range(4):
            # The full decision input identity differs; the truth-relevant state does not.
            receipt = engine._record(
                proposal=None,
                input_fingerprint="sha256:" + str(index) * 64,
                normalized_outcome="SAME_RESULT",
                outcome=outcome,
                before=before,
                after=before,
                attempt_ids=(),
            )
            actual.append(receipt.outcome)
        assert actual == [
            outcome,
            outcome,
            DecisionStepOutcome.REPLAN_REQUIRED,
            DecisionStepOutcome.STOP_PARTIAL,
        ]
        assert runtime.progress.replan_count == 1
    finally:
        manager.close()
