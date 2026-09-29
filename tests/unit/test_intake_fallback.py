"""Deterministic Guided Intake stays narrow when the LLM is unavailable."""

from redteam.intake.fallback import deterministic_intake_turn
from redteam.intake.models import EngagementDraft
from redteam.intake.serializer import merge_draft


def test_fallback_collects_only_explicit_scope_and_budget() -> None:
    draft = EngagementDraft()
    first = deterministic_intake_turn(draft, "https://example.local:8443 테스트해줘")
    draft = merge_draft(draft, first.draft)
    assert draft.allowed_hosts == ["example.local:8443"]
    assert first.follow_up_questions == ["최대 실행 횟수는 몇 회인가요?"]

    second = deterministic_intake_turn(draft, "5")
    draft = merge_draft(draft, second.draft)
    assert second.ready is False
    assert draft.max_actions == 5
    assert draft.allowed_capabilities == []

    third = deterministic_intake_turn(draft, "recon, web")
    assert third.ready is True
    assert third.draft.allowed_capabilities == ["recon", "web"]


def test_fallback_does_not_treat_excluded_host_as_allowed() -> None:
    turn = deterministic_intake_turn(EngagementDraft(), "exclude example.local")
    assert turn.draft.allowed_hosts == []
    assert turn.ready is False
