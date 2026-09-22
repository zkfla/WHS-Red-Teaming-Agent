# Dependency Rules

## The rule

```text
core
 ^
 ├── orchestration
 ├── attack_graph
 ├── event_bus
 ├── control
 ├── evidence
 └── capability

app -> all of the above (composition root only)
```

- `core` imports nothing else in `redteam`.
- Every other module imports `redteam.core.contracts` / `redteam.core.ports`
  — never another module's concrete classes — **except** within a
  team's own modules:
  - Agent Team: `orchestration`, `attack_graph`, `event_bus` may import
    each other.
  - Control Team: `control`, `evidence` may import each other.
- `redteam.app` (the composition root) is the only place allowed to
  import concrete implementations from more than one team's modules in
  the same file.

## Why

So that (instruction.md section 24, all must be YES):

- Agent Team can develop without reading Control Team's code.
- Control Team can develop without reading LangGraph internals.
- Capability Team can build MCP Adapters without reading Agent code.
- Swapping an MCP server / LiteLLM / LangGraph / Event Bus backend /
  Attack Graph backend never requires touching Agent code.
- Every tool execution goes through the Control Plane.
- Every important result becomes Evidence.
- Teams connect only through `core` contracts.

## Enforcement

`tests/unit/test_control_plane_bypass.py` statically parses every
`.py` file's imports (via `ast`) and fails the build if:

1. `redteam.core` imports any other `redteam.*` subpackage.
2. `redteam.orchestration` imports `redteam.capability` or
   `redteam.control` directly (instead of going through
   `redteam.core.ports`).

This is a structural check, not a convention — a PR that violates it
fails CI, not just code review.

## Adding a new module

If you need to import something from another team's package that
isn't already exposed as a `core` port/contract:

1. Open an Issue describing the need.
2. Propose the port/contract addition to PM (Schema Change Proposal —
   see `team-boundaries.md`).
3. PM reviews and merges into `core`.
4. You import the new `core` port/contract, not the other team's
   concrete class.
