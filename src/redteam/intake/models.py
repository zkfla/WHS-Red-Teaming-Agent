"""Structured Intake models.

``EngagementDraft`` is a temporary, all-optional shape (section 5 of the
spec this module implements): it exists only to accumulate partial
answers across turns before ``serializer.py`` converts a *complete* one
into the project's real, already-frozen config models
(``redteam.core.config.EngagementConfig`` /
``redteam.core.contracts.engagement.*``). It deliberately does not
duplicate or replace those models, and it deliberately carries no
secret-shaped field (api key / password / token / cookie) — the Intake
Assistant never sees a secret, by construction, not by runtime
filtering (see ``redteam.app.cli._shared.prompt_secret``).

``allowed_capabilities``/``denied_capabilities`` reuse the canonical
namespace list from ``docs/contracts/actions.md`` — kept here as a
single tuple constant so it is not re-typed in the LLM prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from redteam.core.action_semantics import CANONICAL_CAPABILITIES as BUILTIN_CAPABILITIES
from redteam.core.redaction import redact_sensitive_text

#: Canonical capability namespaces (docs/contracts/actions.md). Not
#: re-declared in `redteam.core` — that vocabulary is fixed by PM there;
#: this is just the one place Intake references it from.
CANONICAL_CAPABILITIES: tuple[str, ...] = tuple(sorted(BUILTIN_CAPABILITIES))


class EngagementDraft(BaseModel):
    """Everything Intake may have collected so far. All fields optional —
    completeness is judged by :func:`missing_required_fields`, not by
    this schema."""

    name: str | None = None
    description: str | None = None

    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_networks: list[str] = Field(default_factory=list)

    #: Human-readable testing focus (e.g. "authorization", "financial
    #: logic") — display/documentation only, never itself an enforcement
    #: mechanism. Recorded on ``Engagement.metadata["focus_areas"]``.
    focus_areas: list[str] = Field(default_factory=list)

    allowed_capabilities: list[str] = Field(default_factory=list)
    denied_capabilities: list[str] = Field(default_factory=list)

    #: Canonical ``"<capability>.<operation>"`` keys only (see
    #: ``redteam.orchestration.action_naming.canonical_action_key``).
    #: Never fuzzy-matched from free text — a value the user only
    #: described in prose belongs in ``notes`` instead, and Preflight
    #: validation (reusing ``redteam.app.cli.commands.validate``'s own
    #: checks) rejects anything here that is not a real canonical key
    #: rather than silently dropping or "best-guessing" it.
    denied_operations: list[str] = Field(default_factory=list)

    #: Maps directly onto ``RuleOfEngagement.availability_impact_allowed``
    #: (e.g. "no DoS" -> ``False``). ``None`` = not yet answered; the
    #: serializer defaults it to ``False`` (fail-closed) once the draft
    #: is finalized, same default the Contract itself uses.
    availability_impact_allowed: bool | None = None

    #: Restrictions stated in prose that do not (yet) map onto an
    #: enforced Contract field or a real canonical operation. Recorded
    #: verbatim on the ROE's ``notes`` field and *always* shown to the
    #: user labeled "documented only, not enforced" — see
    #: ``redteam.app.cli.renderer`` preflight rendering. Never silently
    #: treated as if it were a technical control.
    notes: list[str] = Field(default_factory=list)

    max_actions: int | None = None
    max_tool_calls: int | None = None
    max_duration_seconds: int | None = None

    @field_validator("name", "description")
    @classmethod
    def _redact_text(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value) if value is not None else None

    @field_validator("focus_areas", "notes")
    @classmethod
    def _redact_text_items(cls, values: list[str]) -> list[str]:
        return [redact_sensitive_text(value) for value in values]

    @field_validator("allowed_hosts", "allowed_domains", "allowed_networks")
    @classmethod
    def _scope_has_no_embedded_credentials(cls, values: list[str]) -> list[str]:
        if any("@" in value or "?" in value or "#" in value for value in values):
            raise ValueError("scope entries must be host/domain/network references")
        return values


class IntakeTurn(BaseModel):
    """One Intake Assistant reply: a structured draft update plus the
    conversational bits the CLI shows the user. This whole model is
    what ``LLMClient.complete_structured`` is asked to produce — the
    only thing ever written to disk is the ``draft`` after
    :func:`redteam.intake.serializer.missing_required_fields` finds it
    complete, converted through the real Contract models."""

    draft: EngagementDraft = Field(default_factory=EngagementDraft)
    #: Short natural-language line acknowledging what was just learned
    #: (e.g. "확인했습니다."). Never config data itself.
    acknowledgement: str = ""
    #: Questions for information ``draft`` is still missing. Must not
    #: re-ask about a field already non-empty in the draft passed in —
    #: enforced by the system prompt, not just requested of the model.
    follow_up_questions: list[str] = Field(default_factory=list)
    #: The model's own opinion that no more required info is missing.
    #: The CLI does not trust this alone — see
    #: ``serializer.missing_required_fields`` for the code-side check
    #: that actually gates moving on to Preflight.
    ready: bool = False

    @field_validator("acknowledgement")
    @classmethod
    def _redact_acknowledgement(cls, value: str) -> str:
        return redact_sensitive_text(value)

    @field_validator("follow_up_questions")
    @classmethod
    def _redact_questions(cls, values: list[str]) -> list[str]:
        return [redact_sensitive_text(value) for value in values]


def missing_required_fields(draft: EngagementDraft) -> list[str]:
    """Code-side completeness gate (section 6: LLM proposes, code
    decides). Deliberately not delegated to ``IntakeTurn.ready``."""

    missing: list[str] = []
    if not draft.name:
        missing.append("name")
    if not (draft.allowed_hosts or draft.allowed_domains or draft.allowed_networks):
        missing.append("scope (at least one allowed host/domain/network)")
    if draft.max_actions is None:
        missing.append("max_actions (execution budget)")
    return missing
