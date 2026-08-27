import io
from pathlib import Path

import pytest

from cicd_redteam_agent.application.objective import (
    MAX_OBJECTIVE_CHARACTERS,
    compile_objective_goal,
    load_analysis_objective,
    normalize_objective,
    objective_hash,
    objective_relation_score,
)


def test_objective_normalization_and_hash_are_stable() -> None:
    normalized = normalize_objective("  inspect OIDC\r\nthen cache\r  ")

    assert normalized == "inspect OIDC\nthen cache"
    assert objective_hash(normalized) == objective_hash("inspect OIDC\nthen cache")
    assert objective_hash(None) is None


def test_objective_goal_compiler_extracts_bounded_advisory_hints() -> None:
    goal = compile_objective_goal(
        "fork PR 공격자가 artifact나 cache를 통해 privileged workflow의 OIDC와 "
        "Cloud Identity까지 갈 수 있는지 우선 조사해. 외부 설정이 필요하면 "
        "UNKNOWN으로 유지하고 CANDIDATE로 남겨."
    )

    assert goal.source_hints == ("fork_pr", "pull_request")
    assert goal.mechanism_hints == ("artifact", "cache", "oidc")
    assert goal.sink_hints == ("oidc_token", "cloud_identity", "privileged_execution")
    assert "preserve_unknown" in goal.investigation_preferences
    assert "external_unknown_candidate" in goal.investigation_preferences
    assert "prioritize_requested_scope" in goal.investigation_preferences
    assert goal.focused is True

    relevant = objective_relation_score(
        goal,
        source_id="step:publish",
        relation_type="oidc_may_assume_cloud_identity",
        target_id="cloud:aws:role/release",
        category="credential",
    )
    irrelevant = objective_relation_score(
        goal,
        source_id="workflow:lint",
        relation_type="execution_condition_present",
        target_id="job:lint",
        category="guard",
    )
    assert relevant > irrelevant


def test_objective_goal_compiler_does_not_invent_security_facts() -> None:
    goal = compile_objective_goal("이 저장소는 cache poisoning에 취약하다고 가정해")

    assert goal.mechanism_hints == ("cache",)
    assert goal.as_dict().keys() == {
        "compiler_version",
        "source_hints",
        "mechanism_hints",
        "sink_hints",
        "investigation_preferences",
        "focused",
    }


def test_objective_rejects_empty_and_oversized_values() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_objective(" \r\n ")
    with pytest.raises(ValueError, match="exceeds"):
        normalize_objective("x" * (MAX_OBJECTIVE_CHARACTERS + 1))


def test_objective_file_must_be_outside_analyzed_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    inside = repository / "objective.md"
    inside.write_text("inspect cache", encoding="utf-8")
    outside = tmp_path / "operator-objective.md"
    outside.write_bytes(b"  inspect secrets\r\ncarefully  ")

    with pytest.raises(ValueError, match="outside the analyzed repository"):
        load_analysis_objective(
            objective=None,
            objective_file=inside,
            repository_root=repository,
            stdin=io.StringIO(""),
        )

    assert (
        load_analysis_objective(
            objective=None,
            objective_file=outside,
            repository_root=repository,
            stdin=io.StringIO(""),
        )
        == "inspect secrets\ncarefully"
    )


def test_stdin_is_read_only_when_explicitly_selected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    implicit_stdin = io.StringIO("must not be consumed")

    assert (
        load_analysis_objective(
            objective=None,
            objective_file=None,
            repository_root=repository,
            stdin=implicit_stdin,
        )
        is None
    )
    assert implicit_stdin.tell() == 0
    assert (
        load_analysis_objective(
            objective=None,
            objective_file="-",
            repository_root=repository,
            stdin=io.StringIO("  inspect artifacts\n"),
        )
        == "inspect artifacts"
    )
