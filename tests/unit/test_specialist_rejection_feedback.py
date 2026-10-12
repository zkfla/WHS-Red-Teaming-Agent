"""A proposal rejection must tell the model what to fix, not just that it failed.

Regression for a real session: the model repeated the same invalid
``completion_criteria.kind`` four times (one normal attempt, three repairs) because
the retry feedback never named the allowed set, and the JSON schema offered to the
model for this decision did not declare one either. Both are fixed here; this guards
against either regressing silently back to the generic sentence.

The shared ``CompletionCriterion`` contract itself stays enum-free (other consumers,
notably ``PathPlan``'s permissive storage schema, require that -- see
``test_path_writing.py::test_writing_schema_does_not_modify_permissive_storage_schema``);
the hint is added only to the copy of the schema this planner hands the model.
"""

from __future__ import annotations

from redteam.core.contracts.completion import ALLOWED_CRITERION_KINDS, CompletionCriterion
from redteam.orchestration.specialist_planner import (
    AtomizationDraft,
    SpecialistPlanner,
    StrategyDraft,
    _schema_with_completion_criterion_hint,
)
from redteam.orchestration.specialist_tasks import SpecialistProposalError


def test_the_shared_contract_schema_stays_enum_free():
    schema = CompletionCriterion.model_json_schema()["properties"]["kind"]
    assert "enum" not in schema


def test_the_schema_offered_to_the_model_for_this_decision_declares_the_allowed_kinds():
    for model in (StrategyDraft, AtomizationDraft):
        hinted = _schema_with_completion_criterion_hint(model.model_json_schema())
        kind = hinted["$defs"]["CompletionCriterion"]["properties"]["kind"]
        assert set(kind["enum"]) == ALLOWED_CRITERION_KINDS


def test_the_hint_does_not_mutate_the_schema_it_was_given():
    original = StrategyDraft.model_json_schema()
    _schema_with_completion_criterion_hint(original)
    assert "enum" not in original["$defs"]["CompletionCriterion"]["properties"]["kind"]


def test_an_unlisted_kind_is_still_normalized_not_rejected_so_legacy_data_still_loads():
    criterion = CompletionCriterion.model_validate({"kind": "legacy_value", "description": "x"})
    assert criterion.kind == "unknown" and not criterion.is_known()


def test_completion_condition_rejection_names_the_exact_allowed_kinds():
    error = SpecialistProposalError("specialist_unknown_completion_condition")
    feedback = SpecialistPlanner._rejection(error, "attack_path_proposal").diagnostic.feedback()
    assert feedback.startswith("specialist_unknown_completion_condition at $.questions: ")
    for kind in ALLOWED_CRITERION_KINDS:
        assert kind in feedback
    assert "failed the captured subject" not in feedback


def test_every_rejection_code_the_validator_can_raise_has_actionable_feedback():
    import inspect

    from redteam.orchestration import specialist_tasks

    source = inspect.getsource(specialist_tasks)
    codes = set(__import__("re").findall(r'SpecialistProposalError\("([a-z_]+)"\)', source))
    assert codes  # the extraction itself must find the real raise sites
    for code in codes:
        feedback = SpecialistPlanner._rejection(
            SpecialistProposalError(code), "task_atomization"
        ).diagnostic.feedback()
        assert "failed the captured subject, dependency or condition contract" not in feedback, (
            code
        )


def test_an_unmapped_code_still_falls_back_to_the_generic_sentence_instead_of_crashing():
    error = SpecialistProposalError("some_future_code_not_yet_mapped")
    feedback = SpecialistPlanner._rejection(error, "attack_path_proposal").diagnostic.feedback()
    assert "failed the captured subject, dependency or condition contract" in feedback
