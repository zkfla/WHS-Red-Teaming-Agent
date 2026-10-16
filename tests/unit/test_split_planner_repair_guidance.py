"""Split stage-1 repair feedback: code-owned guidance, then a valid retry commits."""

import json

from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.hypothesis_planner import _REPAIR_GUIDANCE
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_probe_workers import selection as probe_selection
from tests.integration.test_v3_atomization import SplitDouble, _atom_two_tasks, _plan_two_questions


class FeedbackRecordingDouble(SplitDouble):
    """Records the repair feedback that each stage-1 request carries."""

    def __init__(self, plans, atoms):
        super().__init__(plans, atoms)
        self.plan_feedback = []
        self.plan_replays = []

    async def complete_structured(self, *, messages, response_schema, **kwargs):
        if "questions" in response_schema.get("properties", {}):
            content = messages[-1]["content"]
            feedback = content.partition("Previous attempt failed")[2]
            diagnostic, _, replay = feedback.partition("\n\nRejected proposal data:\n")
            self.plan_feedback.append(diagnostic)
            self.plan_replays.append(json.loads(replay) if replay else None)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, **kwargs,
        )


def _continue_without_questions():
    return {"updates": [], "questions": [], "reason": "Keep watching", "termination": "continue"}


def _held_without_resume_conditions():
    return {"updates": [], "questions": [], "reason": "RAW-REASON-MARKER-7d1e wait",
            "termination": "held"}


async def _advance_with(tmp_path, plans):
    llm = FeedbackRecordingDouble(plans, [_atom_two_tasks()])
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        spent = (await session.status())["usage"]["spent"]
    return ids, spent, llm


async def test_continue_without_questions_gets_question_guidance_then_commits(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path, [_continue_without_questions(), _plan_two_questions()],
    )
    assert len(ids) == 2
    assert spent == 3  # rejected plan, valid plan and one atomizer request
    assert len(llm.atom_inputs) == 1
    assert llm.plan_feedback[0] == ""
    assert "Continue is valid only with proposed tasks/questions" in llm.plan_feedback[1]
    assert "Use captured subjects" not in llm.plan_feedback[1]


async def test_held_without_resume_condition_gets_resume_guidance_then_commits(tmp_path):
    ids, spent, llm = await _advance_with(
        tmp_path, [_held_without_resume_conditions(), _plan_two_questions()],
    )
    assert len(ids) == 2 and spent == 3
    assert "$.resume_conditions" in llm.plan_feedback[1]
    assert "resume_condition" in llm.plan_feedback[1]
    assert "RAW-REASON-MARKER-7d1e" not in llm.plan_feedback[1]
    assert llm.plan_replays[1] == {
        "previous_proposal": _held_without_resume_conditions(), "omitted": False,
    }


def test_repair_guidance_fits_diagnostic_limit_and_validates_on_construction():
    for code, (location, guidance) in _REPAIR_GUIDANCE.items():
        assert len(guidance) <= 500, code
        assert safe_feedback(guidance) == guidance, code
        diagnostic = DecisionDiagnostic(
            code=code, stage="hypothesis_planning", kind="validation",
            summary=guidance, retryable=True,
            location=location, validation_issues=[{"rule": code, "location": location}],
        )
        assert diagnostic.feedback().endswith(guidance), code
