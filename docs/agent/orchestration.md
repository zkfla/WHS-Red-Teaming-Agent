# Agent Orchestration

Code: `src/redteam/orchestration/`. Contracts used: `RuntimeState`
(ours), `ActionProposal`, `PolicyDecision`, `ToolInvocation`/`ToolResult`,
`EvidenceRecord`, `EventEnvelope`, `Hypothesis`/`Finding`, `Verdict`.

## The loop

```text
START -> OBSERVE -> PLAN --proposal--> POLICY --ALLOW--> EXECUTE -> VERIFY --+
            ^         |                   |                                 |
            |         |                   \--deny/approval-needed--> UPDATE <+
            |         |                                                |
            |         \--no proposal--> REPLAN <-----------------------+
            |                             |
            \--------- another turn ------+--- terminal --> END
```

| Node | Responsibility |
|---|---|
| OBSERVE | schedule an agent; project the graph into a bounded `GraphObservation` |
| PLAN | frontier -> planner -> one `ActionProposal` (+ `Hypothesis`) |
| POLICY | hand it to `ControlPlane.evaluate`; nothing else |
| EXECUTE | ALLOW-only: build the `ToolInvocation`, call `ToolGateway` |
| VERIFY | append Evidence, then reach a `Verdict` against the expectation |
| UPDATE | fold the outcome back into the Attack Graph |
| REPLAN | loop again, or stop with a named reason |

Three properties this shape is built around:

**Only ALLOW reaches Capability.** `router.route_after_policy` is the
single expression in the codebase that can route to `execute`, and it
reads `phase == EXECUTE`. Orchestration has no import path to a
`ToolGateway` implementation at all.

**A block is recorded, not skipped.** DENY and REQUIRE_APPROVAL route
through UPDATE. Nothing runs, but the attempt and the verdict land in
the graph — otherwise the report cannot say "policy stopped us looking
here", and the planner, seeing no attempt on record, re-proposes the
same action every turn.

**Every exit names itself.** `RuntimeState.termination_reason` is one of
`TerminationReason`: `no_actionable_plan`, `max_steps_reached`,
`budget_exhausted`, `consecutive_failures`, `no_agents_registered`,
`approval_pending`, `user_interrupted`, `execution_error`.
"The run ended" and "the run ran out of budget after two turns" lead to
very different reports.

## Agents: declared playbooks, not free-form loops

```text
AgentSpec.playbook : list[PlanStep]      what this agent may ever propose
available_steps()  : the frontier        which of those the graph supports now
Planner.plan()     : the choice          which single one to propose this turn
```

A planner — including the LLM one — picks from `available_steps`. It
cannot invent a capability, an operation or a tool id that was never
declared, and whatever it picks still goes to the Control Plane. This
narrows what can be proposed; it never widens what is allowed.

A `PlanStep` either names a fixed `target`, or binds one from the graph:

```python
PlanStep(
    step_id="probe-host",
    capability="recon", operation="service_discovery", tool_id=ToolId("recon.ping"),
    target_bind="host",
    preconditions=[NodePattern(type=GraphNodeType.HOST, attrs={"in_scope": True}, bind="host")],
    hypothesis=HypothesisDraft(description="The host is reachable from the harness."),
    expectation=Expectation(kind="output_field_present", field="echo"),
    observed_attrs={"reachable": True},
)
```

The bound form is what makes the loop go somewhere: recon discovers
hosts, and the same step is then offered once per discovered host. A
step is dropped from the frontier once it has been attempted against
that target (`repeatable=True` opts out) — and "attempted" is read from
the Attack Graph, not a conversation buffer, so it survives a checkpoint
restore and is shared across agents.

### Adding an agent

1. Write an `AgentSpec` with its `playbook` in
   `orchestration/agents/registry.py` (or your own module).
2. Add it to the registry — order is the scheduling policy; each agent
   runs until it has nothing left to propose, then hands over.
3. Wire a non-default registry/provider set through the `agents=` and
   `candidate_providers=` bootstrap arguments if it should not be the shipped
   default.

No contract change is needed for any of this.

### Extensible role metadata and candidate compilation

`AgentRegistry` now keeps two deliberately separate collections:

- runnable legacy-compatible `AgentSpec`s;
- `AgentDefinition` plugin metadata for Recon, Web/API, Mobile, Analysis,
  and Reporting.

An empty role is not scheduled merely because its metadata exists. A role
becomes runnable when a candidate provider/playbook is wired, and its cheap
`ActivationRule` must match the current bounded graph observation. Domain
semantics stay in that provider; the registry and scheduler do not understand
finance, mobile, or web business objects.

`CandidateCompiler` converts the current playbook frontier into stable
`ExecutableCandidate`s. It binds a healthy tool from the injected Tool Catalog,
validates arguments against the complete JSON Schema 2020-12 contract, creates a graph-target-based candidate key, and
calculates an explainable deterministic base score. The candidate key excludes
graph revision, mutable score and catalog-selected tool id so harmless graph or
adapter changes cannot manufacture a duplicate action. It includes normalized
arguments, preventing parameter-distinct tests on one target from collapsing
into the same ledger identity. LLM planning still chooses only among the
compiled steps.

### Work ledger and leases

Before PLAN emits an `ActionProposal`, it stores the selected candidate as a
`WorkItem` and atomically acquires a `TaskLease`. UPDATE closes that work as
`SUCCEEDED`, `FAILED`, or `BLOCKED`; an expired lease becomes `STALE`, never a
fake success. `InMemoryWorkStore` is the default and `SQLiteWorkStore` is the
durable local backend. Both enforce:

- at most one active copy of a candidate;
- a non-repeatable candidate remains deduplicated after terminal history;
- a repeatable candidate may run again only after its prior copy is terminal;
- all prerequisites must be `SUCCEEDED`;
- a terminal non-success prerequisite closes its dependent as `BLOCKED` with
  the prerequisite id and status instead of leaving orphaned pending work;
- overlapping opaque conflict keys cannot run together;
- only the lease owner may heartbeat or complete work.

PLAN rebinds a freshly compiled candidate to an existing `PENDING` ledger row
before trying to insert another one. This resumes work left between enqueue and
acquire and lets dependency-waiting work become runnable without defeating
candidate deduplication. While a tool call is in flight, a background heartbeat
renews its lease at a fraction of the TTL. Losing the lease fails closed before
dispatch when possible, and a renewal failure prevents the result from being
promoted as a successful observation.

The ledger stores lifecycle and graph/evidence references only. Security facts
remain in the Attack Graph and Evidence Store.

Candidate history, convergence and report counts are scoped by `run_id`, so a
durable ledger reused by multiple engagements cannot suppress a later run or
mix its work into another report. Opaque conflict keys remain global to the
shared ledger, intentionally preventing two concurrent runs from operating on
the same declared resource at once.

### Planners

`PlaybookPlanner` takes the first available step — deterministic, no
LLM, what most tests run against. `LLMPlanner` asks
`core.ports.llm.LLMClient` (LiteLLM behind it; no model name or provider
SDK appears in agent code) to choose a `step_id` from the candidates it
was shown. A `step_id` that was not offered is refused and the planner
falls back — recorded in `last_fallback_reason`, because a permanently
falling-back LLM planner would otherwise look exactly like a working
one. Provider exception messages and arbitrary returned ids are not retained in
logs or fallback state; only the exception class/rejection category is kept.

The shipped Recon agent uses `planner_kind="llm"` so the LLM path stays
live in the default run. With `FakeLLMClient` it deterministically falls
back, so nothing is flaky.

## Verification: `Verdict` is not `ToolResult.success`

The skeleton mapped `success -> PASS`. `orchestration/expectations.py`
replaces that. An `Expectation` states, *before* the action runs, what
the result would have to look like and whether that outcome is benign or
a violation; it is stored on the `Hypothesis` in the graph, so it cannot
be reshaped around the output afterwards.

| Situation | Verdict |
|---|---|
| tool call failed | `UNKNOWN` — no verdict could be reached |
| no expectation under test | `PASS` |
| expectation met, `violation_if_met` | `VIOLATION` |
| expectation met, benign | `PASS` |
| expectation not met | `PASS` (or `ANOMALY` with `anomaly_if_unmet`) |
| expectation not evaluable | `UNKNOWN`, never a quiet `PASS` |

A `VIOLATION` produces a verified `Finding` carrying the `EvidenceId`
that supports it. `AttackGraphService.record_finding` refuses a verified
finding with no evidence, so an unfalsifiable claim cannot enter the
graph.

Domain providers may declare a typed severity on `HypothesisDraft` before
execution. That value wins when a violation is confirmed; otherwise the
kernel falls back to high for an explicitly security-violating hypothesis and
medium for a generic one. Tool operational risk is never misused as finding
impact.

## State: ids, not objects

`RuntimeState` holds ids and counters (instruction.md §5). Domain state
lives in the Attack Graph and the Evidence Store and is referenced by
id — that is what keeps a checkpoint small enough to write every
super-step. Fields beyond §5 (`termination_reason`, `actions_executed`,
`actions_by_agent`, `consecutive_failures`, `finished_agents`, revision
cursors and the `started_agents` event guard) are all loop bookkeeping;
nothing a security judgement could rest on is in there.

`GraphState` additionally carries transient hand-offs between adjacent
nodes (`observation`, `plan_step`, `action_proposal`, `policy_decision`,
`tool_result`, `last_event_id`). Parallel turns also carry leased
`DispatchItem` values and a reducer map of compact `WorkerOutcome` values;
raw tool output still stays out of the coordinator state. AGGREGATE explicitly
overwrites that reducer map after folding each batch, so long runs and durable
checkpoints do not accumulate every prior worker outcome.

## Bounded parallel dispatch

`max_actions_per_turn=1` preserves the original sequential path. Raising it
enables LangGraph `Send` fan-out, capped again by `global_concurrency` and the
remaining global and per-agent action budgets. The planner's selected candidate
is admitted first;
additional ranked candidates are included only when their conflict keys are
disjoint and their target slots remain below `per_target_concurrency`. Each
worker has isolated transient state and independently traverses
POLICY -> EXECUTE -> VERIFY -> UPDATE.

Policy admission is deliberately serialized because the current Control Plane
owns mutable rate-limit and budget counters. Approved tool invocations execute
concurrently. The WorkLedger heartbeat/lease remains authoritative, and the
aggregate node folds only counters, verdicts and event ids back into
`RuntimeState`.

Gateway/adapter exceptions are normalized identically on sequential and
fan-out paths into a redacted failed `ToolResult`. They still consume the
approved action, produce auditable Evidence and close ledger work as failed;
exception text is never copied into checkpoint, event or graph state. Because
no observation exists to retry or verify, its hypothesis closes inconclusive.

Analysis verifier exceptions follow the same containment rule after the raw
tool Evidence is appended. They produce a redacted `UNKNOWN`, close the
hypothesis inconclusive, fail the WorkItem, and suppress success predicates;
a successful tool transport alone can therefore never masquerade as verified
agent work.

Control Plane exceptions fail closed as a redacted `DENY` with a
`control_error` audit marker. No ToolInvocation is built, and UPDATE closes the
hypothesis and lease through the ordinary blocked path instead of leaving an
orphaned active WorkItem.

## Located context and progress

`ContextProjector` reads the canonical Attack Graph around candidate targets
and applies the active `AgentDefinition.context_profile`. Role declarations
choose node types, neighborhood depth and node/evidence/candidate/token ceilings; the
projector creates no shadow memory. Candidate compilation still uses the full
graph, while only the base-ranked `max_candidates` frontier and bounded
projection reach target-neighborhood traversal and the planner.

A shared bounded LRU caches projections only within the same run namespace and
graph revision.
Checkpoint retries therefore avoid repeating graph traversal, while a new
revision always misses the cache. Cached models are deep-copied on insertion
and retrieval so one planner cannot mutate another's context.

Planner context also carries a bounded list of typed evidence excerpts, but
only when an Evidence record has an explicit `target_node_id` matching an
offered candidate. Untagged evidence is omitted rather than guessed relevant;
raw payloads remain in the Evidence Store and prompts receive ids, provenance,
digest and summaries capped at 240 characters only.

Event wake-ups are burst-coalesced and filtered by run and engagement. A
timeout remains a lossy-bus recovery tick, but a quiescent run is re-entered
only when the canonical graph fingerprint changed; an unchanged idle run does
not consume steps merely because the reconciliation timer fired.

`observation_fingerprint` excludes turn numbers and attempt bookkeeping. The
global fingerprint drives stall detection, while each finished agent stores a
role-scoped fingerprint filtered through its `ContextProfile`; a Host-only
delta therefore does not wake a Mobile-only specialist. If a
repeatable plan keeps executing without changing meaningful graph predicates,
hypotheses or findings, `max_no_progress_turns` terminates it with
`stalled_no_graph_progress`.

Before selection, a conservative branch-pruning rule suppresses only a
repeatable branch that is both high-difficulty and repeatedly failed under the
same stable candidate key. Candidates above the objective-progress override remain
eligible. Every suppression emits `CandidatePruned` with its ledger-derived
reason and counters.

## Specialist extension and terminal roles

An executable specialist is registered as an `AgentDefinition`, `AgentSpec`
and `CandidateProvider`. The provider owns role/domain meaning and emits typed
`CandidateDraft` values; the common compiler still enforces Tool Catalog
binding, schema, health, stable identity and scoring. The default registry now
contains Recon, Web/API and Mobile executable specs. Web/API and Mobile consume
no turn unless their graph activation predicate matches, and produce no action
unless Capability registered the required capability tag.

Current cross-team limitation: Control's scope checker has no scope-checkable
identity for `AssetTarget`, so Mobile can discover/compile an APK or IPA action
but the default Control Plane will deny it. Orchestration does not special-case
or bypass this; Asset scope semantics must be added by the Control/PM contract.

A provider/compiler exception emits the Agent-Team-local
`AgentProviderFailed` event with only its exception class, retires that role's
current frontier, and lets other roles continue. Plugin exception text is not
placed in events or state. Failures of canonical graph/store infrastructure
remain run-level failures handled by runner abandonment.
An explicitly declared but unregistered `provider_id` follows this failure
path; playbook fallback exists only for legacy specs with no definition, so a
configuration typo cannot silently execute different logic.

Runnable roles are ordered by the definition's lower-first `priority_band`
with registration order as a stable tie-break. After a productive turn,
OBSERVE rotates to the next active role instead of draining one role's entire
frontier, preventing broad Recon work from starving newly activated
specialists. A lone active role continues normally.

When fan-out capacity is greater than one, `budget_weight` allocates the
current role's batch share relative to all roles active in the same bounded
observation. This is a scheduling ceiling, not permission: global, per-target,
per-agent and Control Plane limits still apply afterwards.

Verification is behind the independent `AnalysisVerifier` port. The default
implementation reuses the deterministic expectation engine; injected analysis
implementations own the verdict while Findings record their reviewer id and
source references. `ReportingAgent` is read-only: after terminal state it
projects graph, evidence and ledger facts and publishes `ReportProjected`; it
cannot create a new Finding.

The terminal projection also derives comparable run-effectiveness telemetry:
successful work, Evidence and verified Findings per executed action, plus the
dead-end terminal-work ratio. These values come from canonical stores rather
than an LLM score and are included in `ReportProjected`.

Report Findings are also run-scoped by provenance: only Findings whose
`evidence_ids` intersect the current run's Evidence set are projected. A graph
store reused across runs therefore cannot leak an older Finding into the new
report.

The Attack Graph contract itself has no engagement/run namespace on nodes, so
integrity and cleanup reports are still graph-store-wide. The default app
constructs dependencies per engagement; a deployment that shares one graph
store across engagements must add PM/Attack-Graph-owned tenancy semantics.
Orchestration does not infer tenancy from node labels or silently filter those
canonical checks.

`evaluate_convergence` distinguishes clean quiescence from orphaned active
work, unresolved hypotheses and outstanding cleanup. Those conditions end as
explicit failures instead of being mislabeled as successful completion.

`EventWakeup` coalesces relevant Event Bus bursts into one scheduler signal and
turns a timeout into a periodic reconciliation signal. This preserves recovery
when the Event Bus is lossy. `AgentLoopRunner.reconcile_once()` connects one
wake-up to an actual canonical-store re-read and graph run. It revives only
quiescent/reconcilable reasons; budget exhaustion, max-step and repeated-failure
hard stops remain terminal. Repeated hosting of that tick is deployment work.

`REQUIRE_APPROVAL` is recorded as blocked/inconclusive and terminates the
current run as `approval_pending`, retaining the approval id for reporting.
The shared
`ControlPlane` port can evaluate proposals but cannot retrieve or consume a
resolved approval, so trusting an `ActionApproved` event alone would bypass the
mandatory gate. Approval resume waits for that Control-owned contract.

## Checkpointing and replay

`AgentLoopRunner` compiles the graph with an `InMemorySaver` and a
recursion limit derived from `max_steps` — LangGraph's default of 25
super-steps is under four turns of a seven-node loop, so a real run
would otherwise abort for no domain reason.

```python
runner = AgentLoopRunner(deps, settings=OrchestrationSettings(max_steps=20))
final = await runner.run(runtime)

history = await runner.state_history()          # newest first
resumed = await runner.resume_from(history[3].checkpoint_id)
```

Set `checkpoint_backend="sqlite"` and `checkpoint_path=...` in
`OrchestrationSettings` for checkpoints that survive process restarts. The
runner owns the async SQLite connection and exposes `aclose()` plus an async
context manager; application bootstrap closes it in `finally`. SQLite is a
local/development backend, not the intended multi-host production store.
Each runner instance binds to its first `run_id`; a different run requires a
new runner so checkpoint threads and wake-up subscriptions cannot mix.

If graph execution raises or is cancelled, the runner uses the configured
`drain_timeout_seconds` to mark that run's pending and self-owned leased work
as failed via `WorkStore.abandon_run()`, then re-raises the original failure.
It never abandons another run or a lease owned by another worker. Work receives
its hypothesis id at enqueue time, allowing abandonment to close an already
recorded hypothesis inconclusive instead of leaving graph integrity poisoned.

Set `work_backend="sqlite"` and `work_path=...` to wire the durable
`SQLiteWorkStore` at application bootstrap. It uses transactional lease
acquisition and a partial unique index for one active row per candidate;
`memory` remains the default for tests and disposable runs.

Two things to know before relying on replay:

- **The allowlist matters.** LangGraph only reconstructs types on an
  explicit `(module, name)` allowlist; anything else comes back as a raw
  `dict`, silently. `checkpointing.state_type_allowlist()` derives it by
  walking `redteam.core`, `redteam.attack_graph` and
  `redteam.orchestration`, so it cannot go stale when a node puts a new
  model in the state.
- **Rewinding rewinds `GraphState` only.** The Attack Graph, the
  Evidence Store, and anything the target system saw are not rewound —
  by design, since evidence is append-only. A resumed branch continues
  against the world as it is now.

## Settings

`OrchestrationSettings` (ours) is separate from `core.config.OrchestrationConfig`
(the frozen shared contract) and bridges via `from_config`. It carries
`max_steps`, `max_retries`, `max_consecutive_failures`,
`max_no_progress_turns`, observation size, bounded parallelism,
checkpointing, and the recursion limit. Everything in it is
execution policy — no value can widen what the Control Plane allows.

`max_retries`/`retry_count` and `verification_reserve_ratio` remain
compatibility placeholders, not active knobs. The current Analysis verifier is
synchronous and consumes no separate action, while retrying an approved Tool
invocation without a Capability-owned idempotency/budget contract could repeat
side effects or evade rate accounting. The kernel therefore fails/records the
work instead of pretending those settings are safely enforceable.

`app.bootstrap.run_engagement` seeds the engagement scope into the graph
before the loop starts (`orchestration/seed.py`): an empty graph is an
empty frontier, so without it the planner would have nothing to choose
from on turn one. Seeding a host makes it *visible*, not permitted — the
proposal against it is still evaluated.

---

## Tool Catalog compatibility boundary

The frozen shared contracts still expose only `ToolGateway.invoke`; no new
Core port was added. Instead, Agent Team owns a narrow structural
`ToolCatalog` protocol (`get` and `list_by_capability`) which the existing
Capability `MCPToolRegistry` already satisfies. Bootstrap injects that live
registry through `RuntimeDeps`, so discovery/health changes are visible on the
next compile rather than copied into a static per-run descriptor list.

`CandidateCompiler` accepts only the provider's explicit preferred `tool_id`
when that exact descriptor belongs to the canonical capability namespace.
A capability tag alone is not an intent-to-tool binding, so the compiler never
substitutes an arbitrary healthy tool advertising the same tag. After ALLOW,
EXECUTE revalidates action/step equality, namespace, catalog membership and
health before building the `ToolInvocation`; the AI sees only offered
candidates and cannot invent an adapter or server name.

The remaining PM/Capability decision is whether this structural subset becomes
a shared Core port and whether descriptor version, supported operation and
target-kind constraints are added to `ToolDescriptor`. Until then,
orchestration validates the complete available input schema, capability tag
and health but cannot infer descriptor fields the shared contract does not
carry. The proposed action-aware shared port is tracked in
`docs/proposals/tool-selection-port.md`. `ToolDescriptor.risk_level` is
operational safety input for scheduling;
finding severity comes from the pre-execution hypothesis/domain provider, not
from the tool used to test it.

### HTTP integration and observation classification

The built-in HTTP playbooks declare `compatible_tool_ids` for the response
contracts they understand. CandidateCompiler filters the live operation catalog
by those bindings, health and input schema **before** ranking. A browser tool
requiring `url` therefore cannot hide an executable HTTP request requiring
`target_ref` and `path`. DNS resolution is not a substitute for an HTTP response.
New tool contracts need an appropriate playbook/provider and result mapper.

An operator can map a scope hostname to an MCP policy key without changing the
host checked by Control:

```yaml
engagement:
  scope:
    allowed_hosts: [mock-bank]
  metadata:
    target_refs:
      mock-bank: bank-lab
```

The mapping is validated and seeded on the Host node; bound Endpoint requests
reuse it. When omitted, the existing convention that the policy key equals the
hostname remains. The bundled finance AppConfig includes this mapping. Target
refs identify configured targets, not credentials or authorization grants.

Web inspection has `assessment_kind="observation"`. HTTP status and header
presence are stored as graph facts with Evidence IDs. `ObservationRecorded`
links the action and evidence; its verdict is UNKNOWN because no security claim
was tested. Even a verifier returning VIOLATION cannot promote this observation
to a Finding. Missing HSTS by itself is not a verified vulnerability.

Security-check steps retain the existing explicit expectation/verifier path:
VIOLATION produces a verified Finding with stored Evidence IDs and
`metadata.assessment_kind="security_check"`.

### Authentication follow-ups

The Web candidate provider turns an anonymous GET 401 observation into a
hypothesis and a session-bound GET candidate for the same concrete path. Only
operator-registered session references for that host and exact path are eligible:

```yaml
engagement:
  metadata:
    auth_sessions:
      mock-bank:
        - session_ref: alice
          paths: [/api/accounts/alice]
          expected_access: allow
```

The same session reference must exist in the Capability adapter's session
configuration. This metadata contains no passwords, cookies or tokens and does
not grant permission: candidates still pass compilation and Control evaluation.
Without an eligible session or compatible tool, `AuthenticationCheckDeferred`
records the reason. Completed comparisons emit `AuthenticationChecked`; both
events appear in CLI output.

The comparison verifies the anonymous baseline Evidence against the same
engagement, target, method and path, and links baseline and follow-up Evidence.
A 2xx response records session access, 401 records an unaccepted session, and
403 records denied access. Other responses remain inconclusive. With
`expected_access: deny`, a 2xx response is marked `authorization_review_required`.
None of these status-only observations creates a Finding: confirming unauthorized
access still requires a domain-specific content/ownership verification rule.
Each path/session context is tried once in the current work ledger; distinct
session observations count as progress without treating repeated evidence for
the same context as new progress.

The default Web mapper is registered under the executed `web.http_request`
identifier. It uses the invocation's method/path and target port, and preserves
the concrete path separately from its normalized graph ID. VERIFY passes only
the Evidence ID to UPDATE; target and discovered nodes plus discovered edges
carry that provenance. The ID is cleared between sequential/parallel actions.
