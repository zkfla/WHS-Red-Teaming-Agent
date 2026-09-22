"""``EngagementDraft``/``IntakeTurn`` structural guarantees (section 7:
Secret isolation is checked at the schema level, not just by convention).
"""

from __future__ import annotations

from redteam.intake.models import EngagementDraft, IntakeTurn, missing_required_fields

_SECRET_FIELD_NAMES = {"api_key", "password", "token", "cookie", "secret", "credential"}


def test_draft_has_no_secret_shaped_field() -> None:
    field_names = set(EngagementDraft.model_fields)
    assert not (field_names & _SECRET_FIELD_NAMES)
    assert not any("key" in name or "token" in name or "password" in name for name in field_names)


def test_missing_required_fields_reports_each_gap() -> None:
    missing = missing_required_fields(EngagementDraft())
    assert "name" in missing
    assert any("scope" in item for item in missing)
    assert any("max_actions" in item for item in missing)


def test_missing_required_fields_empty_once_satisfied() -> None:
    draft = EngagementDraft(name="X", allowed_hosts=["a.local"], max_actions=10)
    assert missing_required_fields(draft) == []


def test_intake_turn_defaults_to_empty_draft_not_a_validation_error() -> None:
    # A malformed/empty structured response from a provider must not
    # crash the CLI -- it should just look like "nothing new yet".
    turn = IntakeTurn.model_validate({})
    assert turn.draft == EngagementDraft()
    assert turn.ready is False
    assert turn.follow_up_questions == []
