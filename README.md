# WHS Red Teaming Agent

## Project Goal

A multi-agent framework for **authorized security testing** engagements.
This repository is currently a **skeleton**, not a finished product: the
goal of this stage is a shared Repository structure, Core Schema,
Interfaces, and dependency boundaries so 7 developers across 3 teams
(Agent, Control, Capability) plus PM can build in parallel without
breaking each other. See `instruction.md` for the full brief and
`ARCHITECTURE.md` for the architecture decisions behind this layout.

No real exploit logic, payload databases, or attack-tool MCP servers
are implemented at this stage.

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
pip install -e ".[dev]"

# Run the Minimal Vertical Slice end to end
python -m redteam.app.cli run examples/engagement.yaml
# or:
python examples/minimal_run.py

# Tests
pytest -q

# Lint / types
ruff check src tests examples
mypy src/redteam
```

`LiteLLMClient` (`redteam.llm.litellm`) needs Python 3.11+ and the
optional `llm` extra (`pip install -e ".[llm]"`) — the base install and
everything else works on 3.10+, using `FakeLLMClient` by default.

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
  llm/             LLM Gateway implementations (LiteLLM + Fake).
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

1. Implement `MCPAdapter` (`src/redteam/capability/adapters/mcp/base.py`)
   for it under `src/redteam/capability/adapters/mcp/`.
2. Add its container to `deploy/compose/docker-compose.yml` following
   the `mcp-example` naming/health-check pattern (see `deploy/mcp/README.md`).
3. Register its `ToolDescriptor`s and the adapter instance in
   `redteam.app.bootstrap.build_runtime_deps`.

No other code changes — that's the point of the Adapter pattern.

## How to Run Tests

```bash
pytest -q                    # everything
pytest tests/unit -q         # unit
pytest tests/contract -q     # Pydantic schema/contract tests
pytest tests/integration -q  # end-to-end vertical slice
```
