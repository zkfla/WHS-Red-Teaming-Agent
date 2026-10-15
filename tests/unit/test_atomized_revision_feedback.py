"""Copied question fields in atomizer output get a schema rejection; wire-only output commits."""

from types import SimpleNamespace

from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.orchestration.probe_atomization import _task_rejection
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_probe_workers import selection as probe_selection
from tests.integration.test_v3_atomization import TARGET, SplitDouble, _new_candidate_plan


class AtomFeedbackDouble(SplitDouble):
    """Records the repair feedback that each atomizer request carries."""

    def __init__(self, plans, atoms):
        super().__init__(plans, atoms)
        self.atom_feedback = []

    async def complete_structured(self, *, messages, response_schema, **kwargs):
        if "tasks" in response_schema.get("properties", {}):
            content = messages[-1]["content"]
            self.atom_feedback.append(
                content.split("Previous attempt failed", 1)[1]
                if "Previous attempt failed" in content
                else ""
            )
        return await super().complete_structured(
            messages=messages,
            response_schema=response_schema,
            **kwargs,
        )


def _task(*, copied_map=None):
    task = {
        "task_key": "t1",
        "question_id": "q1",
        "objective": "Probe the new premise",
        "title": "New premise probe",
        "role_id": "probe-worker",
        "target_node_ids": [TARGET],
        "completion_criterion_ids": [
            criterion_id(CompletionCriterion(kind="observation", description="Inspect new"))
        ],
        "probe_limits": {"max_actions": 1, "max_model_requests": 4},
    }
    if copied_map is not None:
        task["hypothesis_revisions"] = copied_map
    return {"tasks": [task]}


async def test_copied_revision_map_in_output_is_schema_rejected_then_wire_output_commits(
    tmp_path,
):
    seen_keys = []

    def copied_map(payload):
        seen_keys.append(set(payload["issued_candidate_ids"].values()))
        return _task(copied_map={})

    def wire_only(payload):
        return _task()

    llm = AtomFeedbackDouble([_new_candidate_plan()], [copied_map, wire_only])
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        spent = (await session.status())["usage"]["spent"]
    assert len(ids) == 1 and spent == 3
    assert len(llm.atom_inputs) == 2
    feedback = llm.atom_feedback[1]
    assert "schema_additionalProperties at $.tasks[0]" in feedback
    assert "Remove unsupported fields." in feedback

    issued = {value for keys in seen_keys for value in keys}
    assert not any(key in feedback for key in issued)


def test_long_labels_do_not_truncate_revision_repair_instruction():
    repair = (
        "Copy this question's hypothesis_revisions exactly onto each task of the question: "
        "expected 3 candidate revisions, submitted 2."
    )
    rejection = _task_rejection(
        "atomized_revision_not_preserved",
        index=99,
        task=SimpleNamespace(task_key="t" * 200),
        question=SimpleNamespace(question_id="q" * 200),
        field="hypothesis_revisions",
        differing_fields=[
            "missing_candidate_revisions",
            "extra_candidate_revisions",
            "candidate_revision_values",
        ],
        repair=repair,
    )
    assert repair in rejection.diagnostic.feedback()
    assert len(rejection.diagnostic.feedback()) <= 500
