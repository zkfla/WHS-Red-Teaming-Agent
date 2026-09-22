"""Draft -> real Contract model conversion, merge-patch, and round-trip
(section 5: the LLM never writes YAML; section 6: scope is never
silently widened; section 12: edit patches, never replaces wholesale).
"""

from __future__ import annotations

import pytest
import yaml

from redteam.core.config import load_engagement_config
from redteam.intake.models import EngagementDraft
from redteam.intake.serializer import (
    DraftIncompleteError,
    UnsafeEditError,
    draft_to_engagement_config,
    dump_engagement_yaml,
    engagement_config_to_draft,
    merge_draft,
    scope_diff,
)


def test_merge_draft_keeps_old_field_when_new_is_empty() -> None:
    old = EngagementDraft(name="X", allowed_hosts=["a.local"], max_actions=50)
    new = EngagementDraft(max_actions=200)  # only this changed

    merged = merge_draft(old, new)

    assert merged.name == "X"
    assert merged.allowed_hosts == ["a.local"]  # untouched, not cleared
    assert merged.max_actions == 200


def test_merge_draft_never_silently_widens_scope() -> None:
    """Section 6: even a merge that legitimately replaces the scope list
    replaces it with exactly what the new draft said -- never a superset
    invented by the merge itself."""

    old = EngagementDraft(name="X", allowed_hosts=["127.0.0.1:5000"], max_actions=10)
    new = EngagementDraft(allowed_hosts=["127.0.0.1:5000"])  # LLM re-stated the same value

    merged = merge_draft(old, new)

    assert merged.allowed_hosts == ["127.0.0.1:5000"]


def test_draft_to_engagement_config_requires_completeness() -> None:
    with pytest.raises(DraftIncompleteError) as excinfo:
        draft_to_engagement_config(EngagementDraft())
    assert "name" in excinfo.value.missing


def test_draft_to_engagement_config_maps_availability_impact_and_scope() -> None:
    draft = EngagementDraft(
        name="VulnBank Local",
        allowed_hosts=["127.0.0.1:5000"],
        focus_areas=["authorization", "financial_logic"],
        availability_impact_allowed=False,
        notes=["no data deletion"],
        max_actions=100,
    )

    result = draft_to_engagement_config(draft)
    config = result.engagement_config

    assert config.name == "VulnBank Local"
    assert config.scope.allowed_hosts == ["127.0.0.1:5000"]
    assert config.scope.allowed_domains == []  # never invented
    assert config.execution_budget.max_actions == 100
    rule = config.rules_of_engagement[0]
    assert rule.availability_impact_allowed is False
    assert "no data deletion" in rule.notes
    assert result.unenforced_operations == []


def test_unrecognized_denied_operation_moves_to_notes_not_dropped() -> None:
    draft = EngagementDraft(
        name="X",
        allowed_hosts=["a.local"],
        max_actions=10,
        denied_operations=["web.http_request", "web.not_a_real_operation"],
    )

    result = draft_to_engagement_config(draft)

    assert "web.http_request" in result.engagement_config.rules_of_engagement[0].denied_operations
    assert "web.not_a_real_operation" not in (
        result.engagement_config.rules_of_engagement[0].denied_operations
    )
    assert result.unenforced_operations == ["web.not_a_real_operation"]
    assert "web.not_a_real_operation" in result.engagement_config.rules_of_engagement[0].notes


def test_dump_engagement_yaml_round_trips_through_the_real_loader(tmp_path) -> None:  # noqa: ANN001
    draft = EngagementDraft(name="X", allowed_hosts=["a.local"], max_actions=5)
    config = draft_to_engagement_config(draft).engagement_config

    text = dump_engagement_yaml(config)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)  # real YAML, not a hand-built string

    path = tmp_path / "engagement.yaml"
    path.write_text(text, encoding="utf-8")
    loaded = load_engagement_config(path)
    assert loaded.name == "X"
    assert loaded.scope.allowed_hosts == ["a.local"]
    assert loaded.execution_budget.max_actions == 5


def test_engagement_config_to_draft_round_trips_an_intake_generated_config() -> None:
    original = EngagementDraft(
        name="X",
        allowed_hosts=["a.local"],
        max_actions=10,
        focus_areas=["authorization"],
        denied_capabilities=["mobile"],
    )
    config = draft_to_engagement_config(original).engagement_config

    recovered = engagement_config_to_draft(config)

    assert recovered.name == "X"
    assert recovered.allowed_hosts == ["a.local"]
    assert recovered.max_actions == 10
    assert recovered.focus_areas == ["authorization"]
    assert recovered.denied_capabilities == ["mobile"]


def test_engagement_config_to_draft_refuses_hand_authored_config() -> None:
    config = load_engagement_config_from_text(
        """
        name: "Hand Written"
        scope:
          allowed_hosts: ["a.local"]
        execution_budget:
          max_actions: 5
        rules_of_engagement:
          - name: "custom-rule"
            denied_capabilities: ["mobile"]
        """
    )
    with pytest.raises(UnsafeEditError):
        engagement_config_to_draft(config)


def test_engagement_config_to_draft_refuses_multiple_roe_rules() -> None:
    config = load_engagement_config_from_text(
        """
        name: "Two Rules"
        scope:
          allowed_hosts: ["a.local"]
        execution_budget:
          max_actions: 5
        rules_of_engagement:
          - name: "rule-a"
          - name: "rule-b"
        metadata:
          source: "intake-assistant"
        """
    )
    with pytest.raises(UnsafeEditError):
        engagement_config_to_draft(config)


def test_scope_diff_reports_additions_and_removals() -> None:
    old = EngagementDraft(allowed_hosts=["a.local", "b.local"])
    new = EngagementDraft(allowed_hosts=["b.local", "c.local"])

    diff = scope_diff(old, new)

    added, removed = diff["allowed_hosts"]
    assert added == ["c.local"]
    assert removed == ["a.local"]


def load_engagement_config_from_text(text: str):  # noqa: ANN201
    from redteam.core.config import EngagementConfig

    return EngagementConfig.model_validate(yaml.safe_load(text))
