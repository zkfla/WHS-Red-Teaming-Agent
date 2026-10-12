# Evidence expiry re-judgment (F02) contract

Time passing an observed `valid_until` with no new write event still gives the
planner one bounded re-judgment. Past observations stay true as history; only
current reuse becomes unproven.

## Producer to consumer

`ExpiryWatch.observe` (in `evidence_validity.py`) diffs fresh `project_validity`
projections against the last observation and returns grounded `ValidityChange`
items (claim id, old/new status, reason, related evidence refs).
`recheck_evidence_expiry` (in `app/central_run.py`) queues each change on the
existing central outbox as `EvidenceValidityChanged` with a deterministic event
id, then calls the existing `deliver_outbox`. The row advances
`latest_event_sequence`, so the existing `CentralRun` tick triggers its normal
bounded planner rethink. No new event protocol, timer table, daemon, or state DB.

## Wakeup wiring

`tick` returns `min(next_scan, validity_next)` as the driver deadline, where
`validity_next` is the earliest observed future expiry. `EventWakeup.wait`
accepts the same deadline via `wake_at` and reports `validity_recheck` when it
passes. No busy polling and no per-tick model call: the recheck only re-reads
evidence and graph originals.

## Rules

- Only originals-grounded `valid_until` values participate. Unknown conditions
  get no invented TTL (`next_expiry` returns `None`).
- Duplicate notifications for one observed change coalesce (`ExpiryWatch` plus
  deterministic outbox ids). Re-investigating the same evidence stays allowed;
  unchanged, already-processed expiry never re-notifies.
- Restart recovery reads original conditions into a silent baseline and flushes
  undelivered durable outbox rows; already-delivered rows never redeliver.
- While the mission is not ACTIVE, observations are absorbed and nothing is
  queued: no new model or tool calls, and an expiry notification is never
  scope, approval, or auto-target authorization.
- The 4a final validity gate stays authoritative: expired evidence can never
  confirm completion, and a notification never deletes originals, Findings,
  or task results.


## Late bindings and ack-after-queue

Ticks re-read current originals on any activity or bounded scan, not only
when a previously known deadline arrives, so bindings added after the
baseline (including an earlier deadline that supersedes a far-future one)
are discovered and the earliest deadline is recomputed. A queued change also
triggers the same-tick planner sequence check.

The watch baseline advances only after the change is durably queued: a failed
queue retries instead of dropping the change, while deterministic outbox ids
keep retries duplicate-free. Delivery targets the wrapped inner bus, since
handing the queue-before-notify wrapper to the outbox dispatcher would ack
rows no subscriber ever saw.
Delivery failure never loses the change either: the row stays durable and
later ticks redeliver until the dispatcher reports nothing pending.
