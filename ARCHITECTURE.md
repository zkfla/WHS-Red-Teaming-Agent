# Architecture

This document is the Step 1/2 deliverable from `instruction.md` section
22: architecture decisions, module responsibilities, dependency
direction, potential conflicts, and the final repository tree.

## 1. Architecture Decisions

1. **`redteam.core` is the sole Source of Truth for domain data and
   port interfaces.** Pydantic models under `core/contracts`, Protocols
   under `core/ports`. No framework library (LangGraph, LiteLLM, MCP
   client, docker SDK) is importable from `core`. This is what makes
   every "can we swap X" question in section 24 answerable with YES.

2. **LangGraph is workflow runtime only.** `orchestration.state.RuntimeState`
   carries IDs, not domain objects (instruction.md section 5, copied
   verbatim). A second, LangGraph-only `GraphState` TypedDict wraps
   `RuntimeState` plus **transient** fields (`action_proposal`,
   `policy_decision`, `tool_result`) so adjacent nodes in the same run
   can hand off in-flight objects without a round trip through the
   Attack Graph/Evidence Store just to read back what was just written.
   Only `runtime` is meaningful outside `orchestration`; the transient
   fields are never checkpointed or exposed to other modules. This is a
   deliberate, documented deviation from a strict reading of "LangGraph
   state = IDs only" — the alternative (forcing every node hop through
   a store) adds real complexity for no boundary benefit, since these
   fields never leave the subpackage. Flagged in "Potential conflicts"
   below as a PM decision point.

3. **Every Tool call is structurally gated.** `ActionProposal` ->
   `ControlPlane.evaluate()` -> `PolicyDecision`. Only `orchestration.router.route_after_policy`
   sends the graph to the `execute` node, and only on ALLOW. Enforced
   both at runtime (the router) and structurally
   (`tests/unit/test_control_plane_bypass.py` statically checks that
   `redteam.orchestration` never imports `redteam.capability` or
   `redteam.control`).

4. **Capability Layer is Adapter-pattern, not tool-building.**
   `ToolGateway` -> `MCPToolRegistry` -> `MCPAdapter` -> MCP Server.
   `ExampleMCPAdapter` (in-process, simulated) is a test/dev fixture,
   explicitly opt-in only (`REDTEAM_MCP_SERVERS=example`) — production
   `build_application()` no longer substitutes it when no server is
   configured; an unconfigured deployment gets an honest empty tool
   catalog instead (a later architecture-consolidation pass removed that
   fallback: a misconfigured/unconfigured MCP connection must never look
   like a real tool executed). Real Capability-owned MCP servers exist
   (`src/redteam/capability/servers/`): bounded wrappers around
   `nmap`/ZAP/`nuclei` plus self-contained scoped
   HTTP/recon/proxy/mobile-shell/APK-inspection tools, all
   policy-allowlisted per target, not general attack tooling (still
   respects instruction.md section 21 — no payload databases, no
   unbounded exploit library). See `deploy/mcp/README.md`.

5. **In-memory/filesystem backends everywhere, behind Protocols.**
   `InMemoryEventBus`, `InMemoryAttackGraphStore`, `FilesystemEvidenceStore`.
   Redis/NATS/Kafka/Neo4j are explicitly deferred (section 21) — the
   ports are designed so adding them later doesn't change any caller.

6. **No DI framework.** `redteam.app.bootstrap` is the single
   composition root that imports every team's concrete implementation
   and wires it to `core` ports via plain constructor calls
   (instruction.md section 19).

7. **`requires-python = ">=3.10"`** (not 3.11) — verified against the
   only Python actually available in local dev right now (3.10.4).
   `litellm` is an optional extra (`pip install -e ".[llm]"`), imported
   lazily inside `redteam.llm.litellm.LiteLLMClient` methods so the base
   package, `FakeLLMClient`, and everything else stay importable without
   pulling its large transitive dependency footprint (`boto3`, `openai`,
   `tiktoken`, `tokenizers`, ...). This is *not* a Python-version
   workaround: verified directly (`pip install -e ".[llm]"` then
   `import litellm` on 3.10.4) that current `litellm` releases satisfying
   `litellm>=1.50` install and import fine on 3.10 — an earlier claim
   here that it needed 3.11+ (`typing.NotRequired`) was true of an older
   `litellm` release, not current ones; see
   `docs/adr/0002-litellm-gateway.md`. The `enum.StrEnum` swap noted in
   `enums.py` is an independent, unrelated 3.11+ nicety, not something
   blocked on this.

## 2. Module Responsibilities

| Module | Owner | Responsibility |
|---|---|---|
| `core/contracts` | PM | Pydantic domain schema (Engagement, ActionProposal, PolicyDecision, ToolInvocation/Result, EvidenceRecord, EventEnvelope, Graph Node/Edge/Hypothesis/Finding) |
| `core/ports` | PM | Protocols: `LLMClient`, `EventBus`, `AttackGraphStore`, `ControlPlane`, `EvidenceStore`, `ToolGateway` |
| `core/ids`, `core/enums` | PM | Typed IDs (`NewType`), shared enums |
| `orchestration` | Agent Team | LangGraph state/graph/nodes/router — Observe/Plan/Policy/Execute/Verify/Update/Replan |
| `attack_graph` | Agent Team | `AttackGraphStore` impl(s) + `AttackGraphService` |
| `event_bus` | Agent Team | `EventBus` impl(s) |
| `control` | Control Team | `ControlPlane` impl: scope, policy, rate limit, budget, approval, audit |
| `evidence` | Control Team | `EvidenceStore` impl(s) + `EvidenceService` |
| `capability` | Capability Team | `ToolGateway` impl, `MCPToolRegistry`, `MCPAdapter`s |
| `deploy` | Capability Team | docker-compose, MCP server images, healthchecks |
| `llm` | shared | `LLMClient` impls: `LiteLLMClient`, `FakeLLMClient` |
| `intake` | shared (CLI-facing) | Engagement Intake Assistant: natural-language config authoring only (`EngagementDraft` -> `EngagementConfig`, via `core.ports.llm`) — **not** an Orchestration agent, never touches `ToolGateway`/`ControlPlane`, never executes anything |
| `app` | PM (scaffold) / whoever wires new backends | Composition root (`bootstrap.py`) + CLI (incl. `app/cli/commands/interactive.py`, the `redteam`-no-subcommand entrypoint that calls `intake`, then hands off to `app.bootstrap`/`orchestration` unchanged) |

## 3. Dependency Direction

```text
core
 ^
 ├── orchestration  (+ attack_graph, event_bus — same team, may import each other)
 ├── control        (+ evidence — same team, may import each other)
 └── capability

app -> (all of the above, composition root only)
```

Rules (instruction.md section 4, enforced by `tests/unit/test_control_plane_bypass.py`):

- `core` imports nothing else in this repo.
- Every other module imports `core.contracts` / `core.ports`, never
  each other's concrete classes, **except** within a team's own
  modules (Agent Team's `orchestration`/`attack_graph`/`event_bus`;
  Control Team's `control`/`evidence`).
- `app` is the only place allowed to import concrete implementations
  from more than one team's modules at once.

## 4. Potential Conflicts / PM Decision Points

### Resolved (PM review pass 2)

- **`AppConfig` -> bootstrap wiring.** Resolved: `app.bootstrap.build_application(config, ...)`
  now reads every `AppConfig` section (`LLMConfig` -> `LLMClient`
  choice, `CapabilityConfig` -> tool gateway wiring, `LoggingConfig` ->
  `logging.basicConfig`, `EngagementConfig` -> `Engagement`) and takes
  an explicit override keyword for every dependency
  (`control_plane=`, `tool_gateway=`, `evidence_store=`, `event_bus=`,
  `attack_graph=`, `llm=`) so tests inject `tests.fakes` doubles
  instead of any default it would otherwise build.
  `OrchestrationConfig.max_steps` reaches `RuntimeState.remaining_budget`
  via `run_engagement(..., orchestration_config=...)`.
  `build_runtime_deps`/`load_engagement` (the original, `Engagement`-only
  entry points the CLI still uses) are unchanged. See
  `tests/unit/test_bootstrap_app_config.py`.
- **`GraphNode`/`GraphEdge` `id`/`type` field names — kept as-is.**
  Decision: do not rename. They match instruction.md section 16
  literally, and the `type` builtin shadow is harmless in practice
  (Pydantic handles it fine; not flagged by the configured ruff rule
  set). Noted as a coding-convention caveat in `README.md`
  "Development Rules" rather than changed.
- **`Verdict` vs `ToolResult.success`/`ExecutionStatus` — documented,
  not restructured.** `Verdict` describes what an execution implies
  about a security hypothesis (PASS/VIOLATION/ANOMALY/UNKNOWN);
  `ToolResult.success`/`ExecutionStatus` describe whether the tool call
  itself ran without error. These are independent axes — see
  `redteam.core.enums.Verdict`'s docstring and
  `docs/architecture/execution-flow.md` "Verdict vs ToolResult.success"
  for the worked examples. No new contract (e.g. `VerificationResult`)
  was added — real verification logic is Agent Team's TODO
  (`orchestration.nodes.verify`), reusing `Verdict` as-is.
- **Baseline implementation naming reviewed, not renamed.**
  `DefaultControlPlane` runs real Scope -> Policy -> RateLimit ->
  Budget -> Approval logic (not a stub that just returns ALLOW), so
  "Default" isn't misleading; renaming to `Stub`/`Baseline`/`Minimal`
  would both be less accurate and needlessly disturb existing tests/
  docs. Its docstring now says explicitly that it's the reference
  implementation wired in `app.bootstrap`, not the final production
  policy engine — each incomplete stage (ROE pass-through, minimal
  rule matching) already carries its own `TODO(control-team)`.
  `InMemoryEventBus`/`InMemoryAttackGraphStore`/`FilesystemEvidenceStore`/
  `MCPToolGateway` already name their actual nature accurately; no
  change needed there either.

### Still open

- **`GraphState`'s transient fields** — confirm the team agrees this
  doesn't violate "LangGraph state = IDs only," or require in-flight
  objects to round-trip through stores instead.
- **`requires-python` target** — 3.10 now vs. committing to 3.11+ and
  blocking local dev on an upgrade.

Both are PM decision points, not implementation gaps — left as-is here
rather than resolved unilaterally.

Removed from this list (verified against current code + git history, not
assumed): **scope check semantics** — `control/scope.py` does real CIDR
(`ipaddress`) and domain-suffix matching, not string comparison; and
**`replan_node` always ends the run** — it now loops back to `observe`
with real convergence/stuck-detection (`orchestration.convergence`,
`orchestration.progress`), not a single pass. Both were resolved before
this note was written; the document had simply not caught up. See
`docs/agent/orchestration-implementation-plan.md` for what's current.

## 5. State Ownership

| State | Owner |
|---|---|
| Runtime execution state (`RuntimeState.phase`, `step_count`, ...) | Orchestration (Agent Team) |
| Security domain state (Host/Service/Credential/Finding/...) | Attack Graph (Agent Team) |
| Execution evidence | Evidence Store (Control Team) |
| Engagement policy (scope/ROE/budget/approval) | Control Plane (Control Team) |
| Tool metadata (`ToolDescriptor`, health) | Capability Registry (Capability Team) |
| Shared contract (schema every team reads) | Core (PM) |

No state is owned by two rows at once — e.g. `RuntimeState` never
duplicates a `Finding`, it references one by `FindingId` if/when Agent
Team adds that. See `docs/contracts/attack-graph.md` "Why IDs, not
embedded objects, in RuntimeState."

## 6. Team Integration Points

Canonical version (per-team implementation targets + main contracts
used, in one place): `docs/architecture/team-boundaries.md` "Team
Integration Points". Summary — no new shared schema should be
necessary for any team to start (instruction.md section 28):

```text
Agent Team      -> EventBus, AttackGraphStore, orchestration nodes
Control Team    -> ControlPlane, EvidenceStore
Capability Team -> ToolGateway, MCPAdapter, MCP Registry, Execution Substrate
```

Each of these already has a working (if minimal) implementation from
this PM pass — `DefaultControlPlane`, `MCPToolGateway` +
`ExampleMCPAdapter`, `FilesystemEvidenceStore`, `InMemoryEventBus`,
`InMemoryAttackGraphStore` — so teams are replacing/extending, not
starting from an interface with nothing behind it. `tests/fakes/`
(`FakeControlPlane`, `DummyToolGateway`, `FakeEvidenceStore`, plus
re-exported `InMemoryEventBus`/`InMemoryAttackGraphStore`/`FakeLLMClient`)
gives every team a way to unit-test their own module against the others'
ports without standing up the real thing — see
`tests/fakes/bootstrap.build_test_application()`.

## 7. Repository Tree

```text
repo/
├── README.md
├── ARCHITECTURE.md
├── DEVELOPMENT.md
├── instruction.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── docs/                                    # PM
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── dependency-rules.md
│   │   ├── execution-flow.md
│   │   └── team-boundaries.md
│   ├── contracts/
│   │   ├── events.md
│   │   ├── actions.md
│   │   ├── evidence.md
│   │   ├── attack-graph.md
│   │   ├── config.md
│   │   └── versioning.md   (Contract v0.1 freeze + change procedure)
│   └── adr/
│       ├── 0001-langgraph-runtime.md
│       ├── 0002-litellm-gateway.md
│       └── 0003-mcp-capability.md
│
├── src/redteam/
│   ├── core/                                # PM
│   │   ├── ids.py
│   │   ├── enums.py
│   │   ├── convert.py      (mechanical ActionProposal+PolicyDecision -> ToolInvocation, ToolResult -> EvidenceRecord)
│   │   ├── config.py       (AppConfig / EngagementConfig / LLMConfig / ...)
│   │   ├── contracts/
│   │   │   ├── engagement.py
│   │   │   ├── action.py
│   │   │   ├── target.py   (TargetRef discriminated union)
│   │   │   ├── policy.py   (PolicyDecision + ControlContext)
│   │   │   ├── tool.py
│   │   │   ├── evidence.py
│   │   │   ├── events.py
│   │   │   └── graph.py
│   │   └── ports/
│   │       ├── llm.py
│   │       ├── event_bus.py
│   │       ├── attack_graph.py
│   │       ├── control_plane.py
│   │       ├── evidence_store.py
│   │       └── tool_gateway.py
│   │
│   ├── orchestration/                       # Agent Team
│   │   ├── state.py       (RuntimeState + GraphState)
│   │   ├── context.py     (RuntimeDeps, incl. result_mappers)
│   │   ├── graph.py       (build_graph)
│   │   ├── router.py
│   │   ├── observations/  (Tool Result -> Attack Graph Mutation: GraphMutation,
│   │   │                   ResultMapperRegistry, per-tool_id mappers e.g. recon_http.py)
│   │   └── nodes/
│   │       ├── observe.py
│   │       ├── plan.py
│   │       ├── policy.py
│   │       ├── execute.py
│   │       ├── verify.py
│   │       ├── update.py
│   │       └── replan.py
│   │
│   ├── attack_graph/                        # Agent Team
│   │   ├── service.py
│   │   └── stores/memory.py
│   │
│   ├── event_bus/                           # Agent Team
│   │   └── memory.py
│   │
│   ├── control/                             # Control Team
│   │   ├── service.py
│   │   ├── scope.py
│   │   ├── policy.py
│   │   ├── approval.py
│   │   ├── rate_limit.py
│   │   ├── budget.py
│   │   └── audit.py
│   │
│   ├── evidence/                            # Control Team
│   │   ├── service.py
│   │   └── stores/filesystem.py
│   │
│   ├── capability/                          # Capability Team
│   │   ├── gateway.py
│   │   ├── registry.py
│   │   └── adapters/mcp/
│   │       ├── base.py
│   │       └── example.py
│   │
│   ├── llm/
│   │   ├── litellm.py
│   │   └── fake.py
│   │
│   ├── intake/                              # Engagement Intake Assistant (not an Orchestration agent)
│   │   ├── models.py       (EngagementDraft, IntakeTurn -- temporary, LLM-facing only)
│   │   ├── prompts.py
│   │   ├── assistant.py    (IntakeAssistant, over core.ports.llm.LLMClient)
│   │   └── serializer.py   (draft -> EngagementConfig -> YAML; merge-patch for Edit)
│   │
│   └── app/
│       ├── bootstrap.py
│       └── cli/            (commands/run.py, commands/interactive.py, renderer.py, theme.py, ...)
│
├── deploy/                                  # Capability Team
│   ├── docker/mcp-example/
│   ├── compose/docker-compose.yml
│   └── mcp/README.md
│
├── examples/
│   ├── engagement.yaml
│   ├── app.yaml
│   └── minimal_run.py
│
└── tests/
    ├── fakes/           (FakeControlPlane, DummyToolGateway, FakeEvidenceStore, build_test_application)
    ├── unit/
    ├── contract/
    └── integration/
```

Deviations from instruction.md's suggested tree, and why:

- Added `orchestration/context.py` for `RuntimeDeps` (the dependency
  bundle nodes receive) — not in the original tree, but needed
  somewhere and it's a natural fit next to `state.py`.
- `core/contracts/__init__.py` and `core/ports/__init__.py` re-export
  everything for convenience — this is additive, doesn't change the
  boundary.
- Added `core/convert.py` and `core/config.py` (instruction.md sections
  23 and 19 respectively) — not in the original tree, both fit as
  siblings to `contracts/`/`ports/` since they only ever import from
  `core` itself.
- Added `core/contracts/target.py` for the `TargetRef` discriminated
  union — split out of `action.py` since it's substantial enough (8
  variants) to warrant its own file, and `EvidenceRecord`/future
  contracts may want to reference target kinds too.
- Added `tests/fakes/` (instruction.md section 20) — not in the
  original tree; holds test doubles for every `core` port so any team
  can unit-test against another team's port without standing up the
  real implementation.
- Otherwise the tree matches instruction.md section 15 as given.
