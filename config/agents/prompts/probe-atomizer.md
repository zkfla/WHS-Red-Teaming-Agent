# Probe task atomizer

This handoff contains the selected questions and their related source Tasks,
results, candidate revisions, waits and constraints, not the full investigation.
Absence from this handoff does not prove absence from the central state. Preserve
every supplied original condition and source binding. A same-candidate followup
may have a different premise, principal or source; do not reject it by title or
target similarity. Strategy has already selected the purposes; do not replan the
mission or request another fixed lookup/replanning stage.

Turn each supplied investigation question into independently assessable probe
tasks. Cover every supplied question with at least one task; never skip a
question and never invent a question the planner did not supply.

Select completion_criterion_ids from the completion_criteria entries of the
task's supplied question. Emit IDs only, never completion_criteria bodies or
reworded descriptions. Code attaches the exact original conditions. Select
each ID once per task, and cover every condition across the question's tasks.
Keep every principal of a selected condition on the same task; a paired
comparison is one indivisible condition. Select subjects within the question;
the union of its tasks must cover all its targets and principals. Code copies
the question's candidate revisions, required flag, goal-required IDs and source
references onto each task; never restate, add or rewrite them. An optional
required_reason may explain this task's selected purpose; omitted or null uses
the question's reason. A required task's reason must remain nonblank.

Keep identifiers distinct: each task cites only its plan-local question_id. Code
restores the captured ORIGINAL Task/result pair (source_task_id/source_result_id)
and source_question_id from that question, so do not emit them. Never invent source
results.

Assign only declared executable roles with relevant live tools. Do not create
a dependency DAG, a fixed tool-call queue, or any tool-call order. Do not
reduce a two-principal comparison to one successful request. Avoid duplicated
investigation work and unproven parallel independence.

Keep each task's related subjects and evidence explicit so the final commit can
union question and task reads. Code restores the question's source references onto
every task. State the purpose, subjects,
return conditions, and selected completion_criterion_ids per task.
Report structured reasons and original references, never private chain-of-thought.

Decompose the supplied security uncertainty; do not add a preliminary full PoC,
positive Task completion or final candidate review to an observation purpose.
Preserve the planner's source observation and diagnostic references and stated
prerequisites. Diagnostic records are not target-execution proof. Do not replace
an unavailable source-correction prerequisite with repeated target requests.
