# WHS Red Teaming Agent

## Current runtime

The default runtime uses one central SQLite database and a bounded planner with
two independent workers. The planner proposes investigations; workers choose
tools using native ReAct (`MODEL → TOOLS → SETTLE`). Results and intermediate
evidence can trigger replanning before another worker finishes.

Select a specialist profile and a shared LLM request policy explicitly. The
Web/API and Recon profile is `config/agents/native-specialists-web.yaml`.
Roles describe available expertise; several workers can use the same role.
Changing a profile or operative limit cannot force-resume an existing Run.
See [the runtime guide](docs/agent/orchestration.md) and
[central state contract](docs/contracts/central-state.md).

## Browser setup and automatic web policy

Start `redteam web`, then create or edit an investigation in **조사 선택**.
In the scope step, enter the target URL (including HTTPS and any explicit port)
and choose **사이트 전체 — 정책 자동 생성** or **지정 경로만 — 정책 자동 생성**.
Review the sites and paths, confirm the authorized scope, and save. Existing
investigations keep their manual MCP selection until explicitly edited.

Before the session becomes ready, the application generates and validates the
local web/recon MCP deployment under `<engagement-name>.mcp/<fingerprint>/`
beside the engagement file and connects the policy files automatically.
The selected site remains bound to its scheme, hostname and port. Whole-site
mode permits all literal paths on that origin; exact-path mode permits only the
entered paths. Generated profiles provide anonymous GET/HEAD/OPTIONS requests.
Query, credential, replay and write permissions, Control approval, ROE and
shared budgets remain subject to their existing contracts.

Explicit network CIDRs are retained. Otherwise target addresses are pinned as
individual IPv4/IPv6 networks; a hostname needs a DNS lookup during preparation.
No HTTP request or scan occurs during generation. Source/policy digests and
declarations are saved in engagement metadata and reused on restart; missing
or changed originals are refused. Scope/path changes create a new deployment
and retain old artifacts for previous Runs. Generated MCP settings belong to
that session and do not modify `.env` or the process environment.
**기존 MCP 설정 사용** retains manual/remote selection.

Restart the web server after updating this code; an already-running server
does not reload Python modules automatically.

## Project Goal

A multi-agent framework for **authorized security testing** engagements.
This repository started as a **skeleton** (shared Repository structure,
Core Schema, Interfaces, and dependency boundaries so 7 developers
across 3 teams — Agent, Control, Capability — plus PM could build in
parallel without breaking each other) and has since grown real
Capability-layer implementations on top of that skeleton. See
`instruction.md` for the full brief and `ARCHITECTURE.md` for the
architecture decisions behind this layout.

Capability tools today (`src/redteam/capability/servers/`) are
policy-bounded wrappers around real tools — `nmap` (narrow TCP-connect
port scan, no scripts, disabled by default), a live ZAP daemon (passive
HAR capture only), `nuclei` (`-passive` mode, two fixed local checks, no
arbitrary templates/DSL) — plus self-contained scoped HTTP/recon/proxy
tools with no external binary. No payload databases, no general
exploit/attack-technique library, and no unbounded tool invocation —
every tool call is allowlisted per target/method/path by an
operator-owned policy file and gated by the Control Plane before it can
run at all. See `deploy/mcp/README.md` for the exact tool inventory and
what's still a "candidate, not finalized" external MCP server (Mobile,
Analysis) versus what's real and wired up (Web/API, Recon).

## Architecture

```text
User/Operator -> Engagement Config (Scope/ROE/Limits)
              -> Planner -> investigation Tasks -> bounded InstanceDriver workers
                 Each worker: MODEL -> TOOLS -> SETTLE
              -> Engagement Control Plane (Scope/ROE/Policy/RateLimit/Budget/Approval/Audit) [mandatory gate]
              -> Capability Layer (ToolGateway -> MCPToolRegistry -> MCPAdapter)
              -> Execution Substrate (MCP Server containers)
              -> Target

Original results -> Evidence Store
Tasks/results/executions/graph/request accounting -> central state.sqlite3
Committed outbox -> worker/planner notifications -> replan / criterion review
```

`redteam.core` (contracts + ports) is the Source of Truth. Every other
module depends on `core`; `core` depends on nothing in this repo and no
framework library. See `docs/architecture/overview.md` and
`docs/architecture/dependency-rules.md` for details.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # dev tooling + everything below except a real LLM provider
pip install -e ".[dev,agent]"    # ...add `agent` for a real LLM provider too (one command)

redteam                               # interactive: fixed configuration questions -> Preflight -> run
# -- or the explicit/scripted path --
redteam init                         # writes ./engagement.yaml
redteam validate engagement.yaml     # schema/ROE/capability checks, no target touched
redteam tools health                 # per-MCP-server health
redteam tools list                   # every registered ToolDescriptor
redteam run engagement.yaml          # the Minimal Vertical Slice end to end
redteam status <run-id>
redteam findings list <run-id>
redteam report generate <run-id>

# Tests
pytest -q

# Lint / types
ruff check src tests examples
mypy src/redteam tests
```

`redteam` is the installed console script (`pyproject.toml`
`[project.scripts]`); `python -m redteam.app.cli ...` works identically.
See "CLI" below for the full command reference, including
`REDTEAM_LLM_*`/`REDTEAM_MCP_*` (real LLM provider + real MCP servers) and
the Approval workflow (`redteam approvals approve/deny`).

`LiteLLMClient` (`redteam.llm.litellm`) needs the optional `llm` extra
(`pip install -e ".[llm]"`) to use a real LLM provider — it installs and
imports fine on this project's 3.10+ floor (verified directly; an older
`litellm` release once needed 3.11+, current ones don't). The base
install and everything else works on 3.10+ without it, but an operational
run always needs a real provider: `FakeLLMClient` is a test-only injection
and is rejected for real runs.

## Repository Structure

```text
src/redteam/
  core/            PM — contracts (Pydantic) + ports (Protocols). Source of Truth.
  orchestration/   Agent Team — LangGraph workflow, RuntimeState, nodes.
  attack_graph/    Agent Team — Shared Attack Graph store + service.
  event_bus/       Agent Team — Event Bus backend(s).
  control/         Control Team — Engagement Control Plane.
  evidence/        Control Team — Evidence Store.
  capability/      Capability Team — ToolGateway, MCPToolRegistry, MCPAdapters.
  llm/             LLM Gateway implementations (LiteLLM for runs, Fake for tests).
  app/             Composition root (bootstrap.py) + CLI.

deploy/            Capability Team — docker-compose, MCP server images.
docs/              PM — architecture, contracts, ADRs.
examples/          Runnable engagement config + minimal_run.py.
tests/             unit / contract / integration.
```

Full tree and team ownership: `ARCHITECTURE.md` section "Repository
Tree", and `docs/architecture/team-boundaries.md`.

## Core Concepts

- **ActionProposal** — the only way an Agent asks for a Tool to run.
  Agents never call the Capability Layer directly. Its `target` is a
  `TargetRef` — a `kind`-discriminated union (host/domain/url/endpoint/
  service/principal/asset/graph_node), not a flat dict.
- **ControlContext** — what `ControlPlane.evaluate()` needs beyond the
  proposal itself (engagement, agent, correlation id).
- **PolicyDecision** — ALLOW / DENY / REQUIRE_APPROVAL, produced by the
  Control Plane's code-based gate (not a prompt-based safeguard).
- **ToolInvocation / ToolResult** — normalized shape for every MCP tool
  call and result, regardless of which MCP server produced it. Only an
  ALLOW `PolicyDecision` can become a `ToolInvocation` — enforced
  mechanically by `redteam.core.convert.build_tool_invocation`.
- **EvidenceRecord** — append-only record of what was observed and why,
  correlated back to the `ActionId`/`InvocationId` that produced it.
- **EventEnvelope** — common shape for everything on the Event Bus.
- **Shared Attack Graph** — Host/Service/Endpoint/Principal/Credential/
  Asset/Hypothesis/Finding nodes, referenced by ID from `RuntimeState`
  rather than duplicated into it. Predicates stay until explicitly retracted; each
  retraction records the withdrawn observation lineage and the row version it
  superseded, so a later worker re-observation restores on new evidence while a
  pre-retraction snapshot import stays withdrawn (see `docs/agent/attack-graph.md`).
- **AppConfig / EngagementConfig** — the shared `.env`/YAML config
  shape (`redteam.core.config`) so teams don't each invent their own.

Full schemas: `src/redteam/core/contracts/`, narrative docs:
`docs/contracts/` (includes `config.md`).

## Execution Flow

```text
OBSERVE -> PLAN -> POLICY --ALLOW--> EXECUTE -> VERIFY -> UPDATE -> REPLAN -> END
                     \--deny/require_approval--> REPLAN ---^
```

See `docs/architecture/execution-flow.md` for the step-by-step mapping
to instruction.md's Minimal Vertical Slice (section 16).

## Team Ownership

| Area | Team | Change policy |
|---|---|---|
| Core Contracts / Architecture | PM | PM approval required |
| Orchestration, Attack Graph, Event Bus | Agent Team | Free |
| Evidence Store, Control Plane | Control Team | Free |
| Capability, MCP Adapter, Execution Substrate | Capability Team | Free |

Full table + schema-change process: `docs/architecture/team-boundaries.md`.

## Development Rules

- `core` never imports another `redteam` subpackage or a framework
  library (LangGraph/LiteLLM/MCP/docker). Enforced by
  `tests/unit/test_control_plane_bypass.py`.
- Agents never import Capability/Control internals — only
  `redteam.core.ports.*` Protocols. Same test enforces this for
  `orchestration`.
- Type hints everywhere; Pydantic for every public contract; `Protocol`
  for every port; async-first; no bare `dict`/`Any` on public
  contracts; no circular imports.
- New concrete implementations are wired in exactly one place:
  `redteam.app.bootstrap`.
- Writing a test against another team's port? Use `tests/fakes`
  (`FakeControlPlane`, `DummyToolGateway`, `FakeEvidenceStore`, or
  `tests.fakes.build_test_application()` for a fully-Fake `RuntimeDeps`)
  instead of standing up their real implementation.
- `GraphNode.type`/`GraphEdge.type` intentionally shadow the `type`
  builtin as Pydantic field names (matches instruction.md section 16
  literally; kept deliberately, see `ARCHITECTURE.md` section 4) — fine
  inside these models, just don't reuse `type` as a local variable name
  in code that touches them.

Full rules: `instruction.md` section 20, `docs/architecture/dependency-rules.md`.

## How to Add an Agent

1. Provide the role's definition, prompt and output contract through the existing
   agent package/registry and component assembly.
2. Select the role in the specialist profile, with catalog-based tool selection
   and explicit request/concurrency limits.
3. Let `InstanceDriver` instantiate workers for registered investigation Tasks.
   Shared state belongs to central transactions; each SDK thread holds only its
   own continuation and original execution references.
4. Keep tool execution in the existing admission/Control/gateway/result flow.
   Registering a role or tool does not grant execution permission.

## How to Add a Policy

Add a rule to `redteam.control.policy.evaluate_policy` (or extend
`ApprovalPolicy.require_approval_for` in an engagement's YAML/config —
no code change needed for that case). Keep `DefaultControlPlane`'s
pipeline order (Scope -> ROE -> Policy -> Rate Limit -> Budget ->
Approval) intact; see `src/redteam/control/service.py`.

## How to Add an MCP Server

1. Choose `WebAPIMCPAdapter`, `MobileMCPAdapter`, `ReconMCPAdapter`, or
   `AnalysisMCPAdapter`. All four support Streamable HTTP and local stdio;
   implement `MCPAdapter`
   (`src/redteam/capability/adapters/mcp/base.py`) for a server needing
   custom normalization.
2. Add its container to `deploy/compose/docker-compose.yml` following
   the `mcp-example` naming/health-check pattern (see `deploy/mcp/README.md`).
3. Call `discover_tools()`, atomically register the returned snapshot
   with `MCPToolRegistry.replace_server_tools`. For application startup,
   add the server ID to `CapabilityConfig.mcp_servers` and configure the
   the transport-specific `REDTEAM_MCP_<SERVER_ID>_*` variables documented in
   `deploy/mcp/README.md`.

No other code changes — that's the point of the Adapter pattern.

## How to Run Tests

```bash
pytest -q                    # everything
pytest tests/unit -q         # unit
pytest tests/contract -q     # Pydantic schema/contract tests
pytest tests/integration -q  # end-to-end vertical slice
```

## CLI

`redteam` (`src/redteam/app/cli/`) owns terminal interaction, request routing,
and session lifetimes, but not domain policy. No ROE/Scope/Tool-selection/Approval
policy decision is made in this package. New investigations go through the canonical
`app.bootstrap.build_application(AppConfig)` path (real configured LLM
provider, real configured MCP servers, real Control Plane) — never a
CLI-only shortcut. Resume and approval operations use their existing public APIs;
read-only commands read durable state directly. The default runtime uses the
central Mission/investigation/execution/graph database with two workers.
Evidence, Audit, Approval and SDK checkpoints remain separate original stores;
report files are exports. `status`, `findings` and `report` query current central
state even when a Run is suspended or still executing. The required specialist
profile and shared request policy are operator selections; see
[Central state](docs/contracts/central-state.md#plan2d-completion-cleanup-reports-and-default-cli).

```bash
export REDTEAM_SPECIALIST_PROFILE=config/agents/native-specialists-web.yaml
export REDTEAM_LLM_REQUEST_POLICY_JSON='{"mode":"bounded","max_requests":50,"max_concurrent_requests":2}'
redteam run engagement.yaml --workers 2 --state-db data/orchestration/state.sqlite3
redteam resume <run-id> engagement.yaml --state-db data/orchestration/state.sqlite3
```

The request count above is an example operator budget. Tool, target, token and
time limits still apply. Approval/job waits free worker and model capacity while
retaining the original execution and any occupied resources. Stop closes new
admission and drains within the configured timeout; uncertain calls retain their
resources and are never silently replayed. Resume validates the original scope,
profile, limits, checkpoints, approvals, evidence and usage.

Resource waits automatically continue the original call when another actor
releases its resource/capacity, preserving IDs and the original budget charge.
Identical non-retry-safe UNKNOWN calls cannot be repeated with new Task/call IDs,
even without a provider resource binding. `resume ... --instance <instance-id>`
continues only that instance; it leaves READY siblings and goal planning alone.
Completed selections return without opening SDK state. Live status separates
the current `in_progress` outcome from the previous settlement reason.

Execution uses central state only. The legacy runtime option, old loop and
cohort resume have been removed. Original databases and explicit offline
migration readers remain; an unsupported old Run is refused rather than run
through another engine. New relation-graph and unified-context APIs remain
follow-up work. Historical strategy/activation counters do not limit new work;
actual budgets and concurrent worker/resource limits do.

Providers declare resource effects (including an explicit empty declaration for
read-only operations). Calls with unspecified effects remain visible as
`cleanup_unmapped_tools` and prevent `goal_achieved` until cleanup can be verified.
No resource identity is guessed from response text.

### Interactive setup (`redteam`, no subcommand)

```bash
redteam
```

No YAML, no flags: this reads `engagement.yaml` from the current
directory if it exists (offering `Start`/`Edit`/`New`/`Quit`), or starts
fixed configuration questions if it doesn't. Name, allowed hosts/domains/networks,
objective, allowed capabilities, and action limit are entered separately; no LLM
call interprets these answers. Optional limits and restrictions are available
through the numbered settings menu. The first run also does a one-time LLM setup prompt (skipped
once `REDTEAM_LLM_PROVIDER` is set — see Config below), persisting the
answer to `.env`, never to a YAML file. `Edit` shows a numbered menu; select
one setting, enter its new value, and press Enter at the menu to save. Empty
field input preserves the value; `-` clears an optional setting; `q` cancels.
Existing unrelated scope, approval, rate-limit, rule, and metadata settings
are preserved. Manually authored or multiple-rule configurations still use
the existing direct-YAML editing path. Either way you
land on a Preflight screen (target/scope/focus/budget, enforced ROE vs.
documented-only notes, LLM/config/scope/MCP status) before anything
executes, and `Enter` there hands off to the *exact same*
`build_application`/`run_engagement` path `redteam run` uses below —
nothing is re-implemented for this mode.

The terminal stays open with one editor across Runs. Public Agent decisions,
correlated action/policy/tool results, and a persistent activity/status area
replace the event-per-line panel layout. Ordinary chat is classified **before**
any mutation: read-only question, guidance for the original Run, explicit new
investigation, or clarification. A new investigation waits behind the current
one; it is not silently turned into guidance. Typing across a Run's end keeps
the draft, cursor and original Run attachment. Expired guidance is reported as
undelivered, never redirected to the next Run. Displayed reasoning is a public
decision summary, not private chain-of-thought; detailed assumptions/events are
available in the bounded trace. All actions still pass the existing Control
Plane and MCP path.

| Input | Meaning |
|---|---|
| Natural-language chat | Question / current guidance / new investigation, classified by the LLM |
| `/new <objective>` | Explicit new investigation within the existing validated scope |
| `/status`, `/explain` | Read-only state for the input's Run context |
| `/stop`, `/pause`, `/resume` | Existing Run control APIs; not session exit |
| `approve <ID>`, `deny <ID>` | Explicit decision for the captured pending approval, revalidated against its Run |
| `/pending`, `/cancel <input_id>` | Inspect queued/uncertain requests; cancel only undispatched requests |
| Ctrl+O, `/trace` | Recent redacted details; at most 300 display-history entries |
| Enter / Ctrl+J | Submit / newline (Shift+Enter is not assumed portable) |
| Ctrl+C / Ctrl+D | Clear draft / exit on an empty draft |
| `/exit` | Stop intake, request Run stop, drain outstanding work and close the session |

`/` + Tab completes actual commands. Approval and stop use a separate worker,
so a slow chat classification does not block them. Service acceptance does not
claim that the Agent has already consumed guidance. Uncertain writes are not
automatically retried. `phase`, termination reason, and Mission status are
shown separately; the CLI does **not** reinterpret `completed` as proof of a
successful or exhaustive assessment.

`REDTEAM_CLI_PLAIN=1 redteam` (also selected with `TERM=dumb`) uses a static,
sequential transcript with simple status markers. It does not accept live
steering during execution; suspended Runs remain available for explicit
approval/resume. Redirected stdin uses a sequential fallback. JSON and
`--non-interactive` retain their existing output contracts. The TTY
`redteam run ...` entrypoint uses the same editor but exits after its single Run.
See [CLI session architecture](docs/architecture/cli-session.md) for lifecycle,
race handling, verification, and known boundaries.

Initial configuration asks only for a task description (including target URLs or
addresses) and the desired outcome. Exact host/port scope is read from explicit
addresses; if none is present, the CLI asks for the target separately. The first
target supplies the default name and the configured orchestration step limit
supplies the default action budget (50 by default). Capability types are unrestricted
by default under the existing scope and approval policies. Use **Edit** to adjust
the name, scope, limits, and restrictions. Successful setup proceeds through
preflight without asking for the same objective or another Start confirmation.
The description stays in the existing configuration and planning context; the
saved goal is used unless a subsequent request explicitly overrides it.
Conversion still uses the existing `redteam.intake` draft/serializer contracts.
The LLM remains required for investigation planning and runtime conversation,
but is not called during initial configuration. Additional free-text restrictions are
labeled as recorded instructions, not automatically enforced policy. API keys
are still collected separately by the CLI using `getpass` and stored in `.env`.
The serializer's historical `intake-assistant` source marker remains compatible
with previously generated configuration files.

`--verbose` adds diagnostic event details; `/trace` and Ctrl+O expose recent
safe details without changing policy or execution. For automation, use
`redteam run ... --non-interactive` (and optionally `--json`).

### Config

`engagement.yaml` (target/scope/ROE/budget) is always required.
Everything else — which LLM, which MCP servers — is environment-specific
and comes **only** from the process environment; there is no
application-config YAML file or `--config` flag in this CLI:

```bash
REDTEAM_LLM_PROVIDER=litellm REDTEAM_LLM_MODEL=gpt-4o-mini \
REDTEAM_MCP_SERVERS=recon,web-http \
  redteam run engagement.yaml
```

`REDTEAM_LLM_PROVIDER` has **no implicit default** — unset is a
configuration error, not a silent `FakeLLMClient`. Set it explicitly:
`REDTEAM_LLM_PROVIDER=litellm` with `REDTEAM_LLM_MODEL` for a real provider.
Offline clients must be explicitly injected by tests; `fake` is rejected
for an operational run. LLM reasoning drives investigation paths and actions;
rule candidates provide review context only. `redteam validate` never calls
an LLM at all (see its own `--help`), so this requirement does not apply
to it. Using `litellm` needs the `agent` extra installed
(`pip install -e ".[agent]"`) — see Quick Start. `LLM_API_KEY`/MCP
secrets still come from the process environment (`.env.example`).
`redteam` (every subcommand, not just the interactive entrypoint) loads a
`.env` in the current directory into its own process environment at
startup, best-effort (`_shared.load_dotenv_if_present`) — a real
shell/CI-exported variable always wins over a `.env` value, so this can
only fill in what is otherwise unset, never override an explicit choice.
There are no planning modes: one mandatory LLM flow (path draft, path review,
next action) always proposes from the live MCP catalog, and a failed model
call or an uncompilable draft returns errors for model correction instead of
executing any rule or playbook fallback. Planning never authorizes. Scope,
risk, approval, rate limits and budget are always decided later by the
Control Plane.
The interactive `redteam` entrypoint's one-time setup writes
`REDTEAM_LLM_PROVIDER`/`REDTEAM_LLM_MODEL` and, if you opt in,
`REDTEAM_MCP_SERVERS`/`REDTEAM_MCP_RECON_PASSIVE_*` for a single low-risk
recon-passive server against your first declared host. An explicitly empty
MCP configuration stays empty. Other servers require explicit configuration
and their corresponding devices, captures or artifacts.

`redteam.core.config.AppConfig`/`load_app_config()` (a YAML-loadable
`AppConfig` shape, e.g. `examples/app.yaml`) still exist as a general
library capability for code that calls
`redteam.app.bootstrap.build_application()` directly, outside this CLI
(see `examples/finance_agent_demo.py`) — the CLI itself never reads one.

Runtime settings also have a shared environment path for interactive, `run`, and
`resume`: `REDTEAM_ORCHESTRATION_MAX_STEPS` (default `50`) and
`REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION` (default `true`; LLM reasoning
is mandatory and `false` is rejected). Invalid values fail configuration validation.
The `--max-steps` option overrides the environment value.
`REDTEAM_ORCHESTRATION_MAX_RETRIES` and the configuration keys `max_retries`
and `retry_retryable_tool_failures` are removed and rejected in new inputs.
Central execution returns retryable tool failures to the model for a follow-up
decision. Stored Run settings retain the old values solely for original binding
identity; decoding them does not enable automatic retries.

Before starting another Mission in an interactive session, the CLI reloads and
validates `engagement.yaml`, including scope, ROE, and budget. A missing or invalid
file prevents that Mission from starting; the previous configuration is not reused.

An empty (or unset) `REDTEAM_MCP_SERVERS`/`capability.mcp_servers` is not
an error: `run`, `tools list` and `tools health` all get an honest empty
tool catalog and print an explicit `WARNING` to stderr — no Action can
execute, but nothing pretends a fake tool ran either. The literal value
`REDTEAM_MCP_SERVERS=example` (or `capability.mcp_servers: ["example"]`)
is the one explicit way to opt into the harmless in-process
`ExampleMCPAdapter` test/dev fixture instead — never chosen
automatically. A *configured but broken* MCP server (bad/missing
`REDTEAM_MCP_<ID>_*` env vars) fails the command instead
(`MCPConfigurationError`, exit 2) — see `redteam validate` to catch this
before `run`.

### Commands

```bash
redteam [--verbose]                               # interactive setup/Preflight/run -- see above
redteam init [--output engagement.yaml]          # write a starter Engagement config (from examples/engagement.yaml)
redteam validate engagement.yaml [--json]
                                                   # schema/scope/ROE/capability checks -- never touches a target
redteam run engagement.yaml [--workers 2] [--state-db PATH] [--non-interactive] [--max-steps N] [--json]
redteam resume <run-id> engagement.yaml [--state-db PATH]  # original IDs, checkpoints and accounting
redteam status <run-id> [--state-db PATH] [--json]          # current central state and all waits
redteam roe show engagement.yaml [--json]
redteam tools list [--json]
redteam tools health [--json]
redteam approvals list [--run <run-id>] [--state-db PATH] [--json]
redteam approvals approve <approval-id> [--run <run-id>] [--state-db PATH] [--by <name>]
redteam approvals deny <approval-id> [--run <run-id>] [--state-db PATH] [--by <name>]
redteam approvals resolve <approval-id> approved|rejected --by <name>   # lower-level; approve/deny wrap this
redteam approvals expire --ttl-seconds <n>
redteam findings list <run-id> [--json]
redteam report generate <run-id> [--format md|json] [--output-dir reports]
redteam evidence verify --run <run-id>            # pre-existing, kept as-is
redteam evidence purge --days <n>                 # pre-existing, kept as-is
```

### `--non-interactive` and the Approval workflow

A `REQUIRE_APPROVAL` decision never auto-approves, `--non-interactive` or
not. The difference is what happens next:

- **default (interactive):** `run` prints the pending Approval, then
  polls (`AgentLoopRunner.reconcile_once`, the same reconciliation
  primitive a host loop would use) until a human resolves it with explicit
  `approve`/`deny` in the live session or from another terminal. It resumes
  **the same Run**, preserving the pending action identity. In the new TTY
  editor Ctrl+C clears the draft; `/stop` requests Run stop and `/exit`
  closes the session. Durable safe checkpoints can be reconnected with
  `redteam resume <run-id> engagement.yaml`.
- **`--non-interactive`:** stops at `APPROVAL_PENDING` immediately and
  exits 0 (a correct policy outcome, not a CLI failure — see Exit codes
  below). `redteam approvals approve/deny` still resolves the Approval
  durably; use the explicit `resume` command to continue the safe checkpoint.

```bash
redteam approvals approve appr_abc123
# Approval: appr_abc123
# Run: run_xyz
# Action: web.file_upload
# Target: {...}
# Reason: ...
# appr_abc123 -> approved (by yuyechan)
```

### Exit codes

```text
0 = success (including a correct ROE/Scope DENY, or a still-pending Approval)
1 = a run/command genuinely failed (crash, unreachable MCP mid-run, etc.)
2 = configuration/argument error (bad YAML, unknown ROE operation, bad MCP config, bad CLI args)
130 = interrupted (Ctrl+C)
```

### `--json`

Every command supports `--json` where it produces structured output.
`redteam run --json` streams JSON Lines (one compact object per event,
matching this project's own `.jsonl` evidence/audit file convention) —
every other `--json` command prints one JSON document. Logs and warnings
always go to stderr, so stdout stays parseable either way. Known-secret
key names (tokens, API keys, passwords, ...) are redacted in every
output mode, matching `redteam.control.audit.redact_arguments`.
