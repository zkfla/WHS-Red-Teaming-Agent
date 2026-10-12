# Code artifacts stage E: generation, review, and offline fixtures

Central investigation support for reviewed code DATA plus explicitly opted-in
synthetic validation. Generation, syntax, review acceptance, and fixture
passes never confirm target reproduction, Findings, or goal success.

## Selection and storage

- The selected specialist profile enables the feature only by binding both
  `code_generation` and `code_review` to different registered role IDs.
  A missing pair disables the helpers; a partial or same-role binding fails
  assembly with `code_independent_stage_bindings_required`.
- The composition root selects a separate immutable blob store only for the
  enabled feature (default under the evidence directory parent, never inside
  evidence/audit/approval stores). Injection without selected code roles is
  refused; a custom injected store needs an explicit declared location plus
  a stable 64-hex fingerprint.
- The fixture runner stays explicit-only: an operator policy path or an
  injected runner, never both, never a silent host fallback. Unconfigured
  means `fixture_runner_unconfigured`; there is no default enablement.

## Binding and resume compatibility

- The immutable scope binds Run, mission, engagement, task, instance, task
  status/owner/version/agent, the task completion contract, the mission goal
  revision and goal hash, the delegated purpose, the profile fingerprint,
  and the artifact store fingerprint. Fixture validation additionally binds
  the exact artifact digest and the runner fingerprint.
- A changed backend, path, policy, prompt version, owner, goal, or purpose
  refuses the same Run or admission instead of adopting stale output. Past
  originals are retained as history; there is no auto-migration.
- Resuming an old Run whose profile selected code roles but whose runtime
  kept the feature inactive stays disabled; adopting the feature needs a
  new Run. Previously collected originals remain collectible.

## Review, fixtures, and goal gating

- Generation and independent review share one caller request/token/time
  budget inside the Run metered scope. Review must echo the exact artifact
  digest; rejection, digest mismatch, and budget exhaustion are structured
  results, never silent success.
- `code.validate` checks only the reviewed artifact bytes through the
  configured runner. Same-request redelivery reuses the recorded result;
  changed inputs never rerun silently. Interrupted admission without a
  collected result stays held.
- Goal success requires verified fixture cleanup: a held or
  cleanup-unverified result, or an admission without a collected result,
  blocks the goal in `goal_inputs` and the final commit rechecks
  admissions atomically. A recorded failure with verified cleanup permits
  success. Fixture rows are synthetic validation originals and never count
  as target success evidence.

## Verification

- `tests/unit/test_plan4e_code_artifacts.py`: stage selection, schema
  bounds, secret/syntax/size rejection, registry descriptors, and the
  fixture-runner factory contract. No state, Docker, target, or model.
- `tests/integration/test_plan4e_code_artifacts.py`: scripted
  author/reviewer plus an explicit synthetic runner fake against a real
  central store. Covers pass/reuse flows, independent review, rejection
  paths, tamper/race handling at the SQL commit boundary, and the goal
  cleanup gate. No Docker, target, or paid model calls.
