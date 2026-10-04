# WHS Red Teaming Agent

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
              -> Agent Orchestration Layer (LangGraph: Observe->Plan->Policy->Execute->Verify->Update->Replan)
              -> Engagement Control Plane (Scope/ROE/Policy/RateLimit/Budget/Approval/Audit) [mandatory gate]
              -> Capability Layer (ToolGateway -> MCPToolRegistry -> MCPAdapter)
              -> Execution Substrate (MCP Server containers)
              -> Target

All results -> Evidence Store, Event Bus, Shared Attack Graph
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
  rather than duplicated into it.
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

1. Add a node function `async def my_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState`
   under `src/redteam/orchestration/nodes/`.
2. Register it in `redteam.orchestration.graph.build_graph` and route to
   it from `redteam.orchestration.router`.
3. Only touch `state["runtime"]` (the `RuntimeState` contract) for
   anything that must outlive the node; use `GraphState`'s other
   (transient) keys for in-flight objects between adjacent nodes.
4. To propose a tool call, build an `ActionProposal` and call
   `deps.control_plane.evaluate(...)` — never `deps.tool_gateway`
   directly.

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
read-only commands read durable state directly
(Approval/Audit/Evidence stores, or the persisted terminal report — see
`redteam.app.report_store`).

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
`resume`: `REDTEAM_ORCHESTRATION_MAX_STEPS` (default `50`),
`REDTEAM_ORCHESTRATION_MAX_RETRIES` (default `3`), and
`REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION` (default `true`; LLM reasoning
is mandatory and `false` is rejected). Invalid values fail configuration validation.
The `--max-steps` option overrides the environment value.

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
redteam run engagement.yaml [--non-interactive] [--max-steps N] [--json]
redteam resume <run-id> engagement.yaml          # reconnect a safe durable checkpoint
redteam status <run-id> [--json]                  # from the persisted terminal report; "unknown" if still running elsewhere
redteam roe show engagement.yaml [--json]
redteam tools list [--json]
redteam tools health [--json]
redteam approvals list [--run <run-id>] [--json]
redteam approvals approve <approval-id> [--by <name>]
redteam approvals deny <approval-id> [--by <name>]
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
