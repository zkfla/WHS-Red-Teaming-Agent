# collect-originals CLI and central job.reconcile (plan 4d)

Explicit, collection-only entries into a central Run. Neither resumes actors,
completes Tasks/goals, releases claims, nor settles budgets.

## collect-originals

- `redteam collect-originals <run_id> <engagement.yaml> [--instance ID] [--json] [--state-db PATH]`
- Reads the original Run central state, stored settings/component binding, and
  Evidence/Audit/Approval/checkpoint paths first; missing stores are refused,
  never created (no empty DB, no new checkpoint file).
- Takes the existing Run owner, requires quiescence, rechecks the activation
  inventory under ownership, and collects by original invocation/action/job
  reference only via `InstanceDriver.collect_checkpointless_originals`.
- `--instance` scopes inventory, checkpoint probes, and collection to that
  actor; siblings are never read or probed.
- Results: `known` (collected), `held` (missing/unknown, with reason),
  `invalid` (digest/Run/action/invocation/tool/input/job mismatch: rejected).
- Repeats reuse immutable originals/receipts; same ID with different content
  is an identity conflict. JSON output is masked; exit 0 only when every
  collected original is `known`.

## job.reconcile (central)

- No-arg call lists masked `{job_ref, invocation_id, action_id, task_id,
  instance_id, state, wait_reason}` rows for the current Run+instance only,
  plus long-job adapter availability.
- `job_ref`/`invocation_id` collects one registered job through receipt
  verification and the existing commit contract. Active/UNKNOWN stays held;
  the same job is never resubmitted and Task/goal success is never confirmed.
- `work_id` is legacy-only: on a central Run it is an explicit compatibility
  error naming `job_ref`/`invocation_id`.

## Boundaries

- No new target tool calls, job submissions, model judgments, worker starts,
  approval grants, or graph/Finding promotion on either path.
- Checkpoint-less collection never auto-resumes actors or settles usage.
- SQL is never held while waiting on provider status/results.
