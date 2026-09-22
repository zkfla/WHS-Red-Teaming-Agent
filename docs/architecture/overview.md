# Architecture Overview

See `ARCHITECTURE.md` at the repo root for the full decision record.
This page is the short version for onboarding.

## Layers

```text
User/Operator
  -> Engagement Config (Scope / ROE / Limits)
  -> Agent Orchestration Layer (LangGraph: Observe->Plan->Policy->Execute->Verify->Update->Replan)
  -> Engagement Control Plane   [mandatory gate before any tool call]
  -> Capability Layer (ToolGateway -> MCPToolRegistry -> MCPAdapter)
  -> Execution Substrate (MCP Server containers)
  -> Target

All results -> Evidence Store, Event Bus, Shared Attack Graph
```

## The one rule that matters most

`redteam.core` (`core/contracts` + `core/ports`) is the Source of
Truth. It imports nothing else in this repo and no framework library.
Everything else imports `core`, never the reverse. See
`dependency-rules.md`.

## Why this shape

Seven people, three teams, one repo, parallel development. The
question this architecture answers for each team is: *what do I need
to know about the other teams to start coding today?* Answer: only the
Pydantic models in `core/contracts` and the Protocols in `core/ports`.
Nothing else.
