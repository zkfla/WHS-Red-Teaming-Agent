"""Typed planner progress and the configured replan cap, without any model or database."""

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.orchestration.hypothesis_planner import HypothesisDecision
from redteam.orchestration.planner_progress import (
    REPLAN_EXHAUSTED_CODE,
    REPLAN_REQUESTED_REASON,
    PlannerProgressStatus,
    replan_exhausted_diagnostic,
)
from redteam.orchestration.probe_atomization import HypothesisPlanDraft
from redteam.orchestration.settings import OrchestrationSettings


def test_declared_kinds_carry_only_their_own_fields():
    assert PlannerProgressStatus(kind="work_proposed").replan_count == 0
    assert PlannerProgressStatus(kind="existing_work", replan_limit=3).replan_limit == 3
    assert PlannerProgressStatus(kind="held").diagnostic is None
    queued = PlannerProgressStatus(
        kind="replan_queued",
        replan_count=1,
        replan_limit=3,
        cause_code=REPLAN_REQUESTED_REASON,
    )
    assert queued.model_dump(mode="json")["cause_code"] == REPLAN_REQUESTED_REASON


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "replan_queued", "replan_count": 1, "replan_limit": 3},
        {
            "kind": "replan_queued",
            "replan_count": 1,
            "replan_limit": 3,
            "cause_code": REPLAN_REQUESTED_REASON,
            "diagnostic": replan_exhausted_diagnostic(accepted=1, limit=3),
        },
        {"kind": "replan_exhausted", "replan_count": 3, "replan_limit": 3},
        {
            "kind": "replan_exhausted",
            "replan_count": 3,
            "replan_limit": 3,
            "diagnostic": replan_exhausted_diagnostic(accepted=3, limit=3).model_copy(
                update={"retryable": True}
            ),
        },
        {"kind": "held", "cause_code": REPLAN_REQUESTED_REASON},
        {"kind": "work_proposed", "replan_count": 4, "replan_limit": 3},
        {"kind": "continue"},
        {"kind": "held", "unexpected": True},
    ],
)
def test_inconsistent_projections_are_refused(fields):
    with pytest.raises(ValidationError):
        PlannerProgressStatus.model_validate(fields)


def test_exhausted_diagnostic_is_concrete_nonretryable_and_bounded():
    diagnostic = replan_exhausted_diagnostic(accepted=3, limit=3)
    assert diagnostic.code == REPLAN_EXHAUSTED_CODE
    assert diagnostic.kind == "no_progress" and diagnostic.stage == "hypothesis_planning"
    assert diagnostic.retryable is False
    assert (diagnostic.attempts, diagnostic.attempt_limit) == (3, 3)
    assert 0 < len(diagnostic.summary) <= 500
    assert 0 < len(diagnostic.repair_condition) <= 500


def test_round_trip_through_the_store_payload_shape_is_stable():
    status = PlannerProgressStatus(
        kind="replan_exhausted",
        replan_count=2,
        replan_limit=2,
        diagnostic=replan_exhausted_diagnostic(accepted=2, limit=2),
    )
    restored = PlannerProgressStatus.model_validate(status.model_dump(mode="json"))
    assert restored == status


def test_replan_cap_default_bounds_and_identity_are_explicit():
    assert OrchestrationConfig().planner_replan_max_passes == 3
    assert OrchestrationSettings().planner_replan_max_passes == 3
    for value in (0, 32):
        assert (
            OrchestrationConfig.model_validate(
                {"planner_replan_max_passes": value}
            ).planner_replan_max_passes
            == value
        )
        assert (
            OrchestrationSettings.model_validate(
                {"planner_replan_max_passes": value}
            ).planner_replan_max_passes
            == value
        )
    for value in (-1, 33):
        with pytest.raises(ValidationError):
            OrchestrationConfig.model_validate({"planner_replan_max_passes": value})
        with pytest.raises(ValidationError):
            OrchestrationSettings.model_validate({"planner_replan_max_passes": value})


def test_replan_cap_reads_its_environment_name_and_enters_both_identities():
    config = OrchestrationConfig.from_environment({"REDTEAM_PLANNER_REPLAN_MAX_PASSES": "2"})
    assert config.planner_replan_max_passes == 2
    settings = OrchestrationSettings.from_config(config)
    assert settings.planner_replan_max_passes == 2
    assert settings.central_payload()["planner_replan_max_passes"] == 2
    assert settings.durable_payload()["planner_replan_max_passes"] == 2


def test_retired_no_progress_setting_stays_passive_and_separate():
    settings = OrchestrationSettings()
    assert not hasattr(settings, "max_no_progress_turns")
    assert settings.central_payload()["max_no_progress_turns"] == 3
    assert settings.planner_replan_max_passes == 3


def test_model_output_cannot_carry_the_trusted_controls():
    base = {"reason": "Continue with the captured evidence", "termination": "replan"}
    controls = (
        ("replan_max_passes", 9),
        ("witness_inputs", {"eligible_roles": ["probe-worker"], "require_probe_limits": False}),
    )
    for key, value in controls:
        for schema in (HypothesisDecision, HypothesisPlanDraft):
            with pytest.raises(ValidationError) as caught:
                schema.model_validate({**base, key: value})
            assert any(
                item["type"] == "extra_forbidden" and item["loc"] == (key,)
                for item in caught.value.errors()
            )
