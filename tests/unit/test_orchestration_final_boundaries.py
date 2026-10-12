"""Terminal reason preservation, safe failure feedback, and ordered recovery."""


from redteam.orchestration.completion import CompletionIssues


def test_completion_correction_never_copies_legacy_raw_error_input():
    from redteam.orchestration.cognition import DecisionBudget
    from redteam.orchestration.engine import InvestigationEngine

    issues = CompletionIssues()
    secret = "credential-with-no-label-123456"
    issues.add(
        f"evidence '{secret}' belongs to a different run",
        code="completion_evidence_run",
        summary="The evidence must belong to this run.",
    )
    budget = DecisionBudget(3)
    feedback = InvestigationEngine._safe_errors(issues, budget)
    assert secret not in str(feedback)
    assert secret not in budget.last_failure.model_dump_json()
    assert budget.last_failure.code == "completion_evidence_run"
