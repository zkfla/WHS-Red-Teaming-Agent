"""Bounded execution-feedback window: merge, cap, replay, and old-checkpoint rules."""

from __future__ import annotations

import json

from redteam.core.ids import EngagementId, RunId
from redteam.orchestration.runtime_observation import (
    MAX_RUNTIME_OBSERVATIONS,
    RuntimeObservation,
    append_runtime_observation,
    observations_for_prompt,
)
from redteam.orchestration.state import RuntimeState

RUN = RunId("run_feedback_window")
ENGAGEMENT = EngagementId("eng_feedback_window")


def _runtime(**overrides):
    return RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT, **overrides)


def _obs(kind="control", outcome="deny", step=0, action="action_1", **overrides):
    fields = {
        "kind": kind,
        "summary": kind + " " + outcome + " for " + action,
        "outcome": outcome,
        "step": step,
        "action_id": action,
    }
    fields.update(overrides)
    return RuntimeObservation(**fields)


def test_window_keeps_only_the_newest_twelve() -> None:
    runtime = _runtime()
    for index in range(MAX_RUNTIME_OBSERVATIONS + 2):
        runtime = append_runtime_observation(
            runtime, _obs(action="action_" + str(index), step=index)
        )
    assert len(runtime.recent_observations) == MAX_RUNTIME_OBSERVATIONS
    assert runtime.recent_observations[0].action_id == "action_2"
    assert runtime.recent_observations[-1].action_id == "action_13"


def test_same_key_refreshes_in_place_with_merged_lists() -> None:
    runtime = append_runtime_observation(
        _runtime(), _obs(reasons=["first"], matched_rules=["rule_a"])
    )
    refreshed = append_runtime_observation(
        runtime,
        _obs(
            reasons=["second"],
            matched_rules=["rule_b"],
            summary="newest summary",
            approval_id="appr_new",
        ),
    )
    assert len(refreshed.recent_observations) == 1
    entry = refreshed.recent_observations[0]
    assert entry.summary == "newest summary"
    assert entry.reasons == ["first", "second"]
    assert entry.matched_rules == ["rule_a", "rule_b"]
    assert entry.approval_id == "appr_new"


def test_missing_approval_keeps_the_stored_reference() -> None:
    runtime = append_runtime_observation(_runtime(), _obs(approval_id="appr_old"))
    refreshed = append_runtime_observation(runtime, _obs(summary="newest summary"))
    assert len(refreshed.recent_observations) == 1
    assert refreshed.recent_observations[0].approval_id == "appr_old"
    assert refreshed.recent_observations[0].summary == "newest summary"


def test_identical_replay_changes_nothing() -> None:
    runtime = append_runtime_observation(_runtime(), _obs(reasons=["only"]))
    replayed = append_runtime_observation(runtime, _obs(reasons=["only"]))
    assert replayed is runtime
    assert len(replayed.recent_observations) == 1


def test_subset_replay_changes_nothing() -> None:
    runtime = append_runtime_observation(
        _runtime(), _obs(reasons=["kept", "also_kept"], summary="stable")
    )
    replayed = append_runtime_observation(runtime, _obs(reasons=["kept"], summary="stable"))
    assert replayed is runtime


def test_approval_wait_and_allow_stay_as_separate_entries() -> None:
    runtime = append_runtime_observation(
        _runtime(),
        _obs(outcome="require_approval", approval_id="appr_7", requires_approval=True),
    )
    runtime = append_runtime_observation(runtime, _obs(outcome="allow"))
    assert len(runtime.recent_observations) == 2
    assert runtime.recent_observations[0].outcome == "require_approval"
    assert runtime.recent_observations[0].approval_id == "appr_7"
    assert runtime.recent_observations[1].outcome == "allow"


def test_reason_and_rule_lists_keep_the_most_recent_caps() -> None:
    old = _obs(
        reasons=["reason_" + str(index) for index in range(8)],
        matched_rules=["rule_" + str(index) for index in range(12)],
    )
    runtime = append_runtime_observation(_runtime(), old)
    new = _obs(
        reasons=["reason_6", "reason_7", "fresh_0", "fresh_1"],
        matched_rules=["rule_10", "rule_11", "fresh_rule_0", "fresh_rule_1"],
    )
    refreshed = append_runtime_observation(runtime, new)
    entry = refreshed.recent_observations[0]
    assert entry.reasons == ["reason_" + str(index) for index in range(2, 8)] + [
        "fresh_0",
        "fresh_1",
    ]
    assert entry.matched_rules == ["rule_" + str(index) for index in range(2, 12)] + [
        "fresh_rule_0",
        "fresh_rule_1",
    ]


def test_prompt_rendering_drops_empty_values() -> None:
    rendered = observations_for_prompt([_obs(capability="recon")])
    assert len(rendered) == 1
    assert all(value is not None for value in rendered[0].values())
    assert rendered[0]["kind"] == "control"
    assert rendered[0]["action_id"] == "action_1"
    assert "arguments" not in rendered[0]
    assert "evidence" not in rendered[0]


def test_checkpoint_without_the_window_loads_empty_and_keeps_references() -> None:
    runtime = _runtime(pending_action_id="action_9", pending_approval_id="appr_9")
    runtime = append_runtime_observation(runtime, _obs())
    assert len(runtime.recent_observations) == 1
    old_payload = runtime.model_dump(mode="json", exclude={"recent_observations"})
    assert "recent_observations" not in old_payload
    restored = RuntimeState.model_validate(json.loads(json.dumps(old_payload)))
    assert restored.recent_observations == []
    assert restored.pending_action_id == "action_9"
    assert restored.pending_approval_id == "appr_9"
