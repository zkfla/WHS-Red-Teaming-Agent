# ADR 0004: OpenCode Go Adapter Behind the LLMClient Port

## Status

Accepted.

## Context

Operators need selectable OpenCode Go support (Chat Completions, Responses,
and Anthropic Messages) through the existing CLI and LLM port, without a new
framework, model catalog, credential database, or per-role model selection.
ADR-0002 keeps `redteam.core.ports.llm.LLMClient` as the only interface
Agent/Orchestration code depends on. This decision keeps provider details
in the LLM adapter and its SDK-free configuration helper.

## Decision

- Provider name `opencode-go`, selected through the existing trio
  (`REDTEAM_LLM_PROVIDER` / `REDTEAM_LLM_MODEL` /
  `REDTEAM_LLM_API_KEY_ENV_VAR`). Zero `LLMConfig` schema change: the same
  three fields carry the Go selection, and no multi-profile system is added.
  `core/config.py` stays feature-agnostic; Go vocabulary lives in the
  feature-owned SDK-free `redteam.llm.opencode_go_config`.
- Explicit SDK-qualified model: `openai/<id>`, `openai/responses/<id>`, or
  `anthropic/<id>`. The qualifier selects the wire protocol; no hardcoded
  model-name catalog is maintained. Endpoints are fixed adapter-owned
  constants -- no configurable endpoint field exists anywhere.
- Key reference `OPENCODE_API_KEY`: onboarding writes it and persists
  `REDTEAM_LLM_API_KEY_ENV_VAR=OPENCODE_API_KEY`, but an already-configured
  `api_key_env_var` is preserved, never silently overwritten. Doing nothing
  in onboarding still yields `litellm`.
- Bootstrap (`redteam.app.bootstrap.build_llm_client`) selects the Go
  factory (`from redteam.llm.opencode_go import build as build_opencode_go`)
  for the Go provider, after an offline shape check via the shared
  feature-owned helper (`redteam.llm.opencode_go_config.validate_model`).
  Validation and construction share one parser. The existing `litellm`
  factory behavior remains intact and explicit injection still wins at
  the composition root.
- The generic LiteLLM adapter accepts an explicit Responses transport
  selection. Go's Responses models use `aresponses` directly because the
  SDK's Chat conversion can lose the original unsuccessful terminal
  status. API-key Responses default to non-streaming and retain usage;
  the existing ChatGPT subscription keeps its streaming route.
- Request identity lives in core and is re-exported by orchestration.
  Go derives the session header from the stable Run/owner identity at
  each call. An uncheckpointed conversation uses a client-owned identity.
  Messages adds the required Bearer header; LiteLLM also sends its
  Anthropic `x-api-key` header with the same key to the same Go endpoint.
- Dependency floor: `llm`/`agent` extras stay at `litellm>=1.50` (no
  pyproject change, no new extra). The Go adapter performs an explicit
  runtime minimum-version check (`>=1.104.0`) with an actionable message.
- `redteam validate` gains an additive offline `check_llm` step (no
  `require_llm` flip, zero provider calls): ordinary parsing via
  `LLMConfig.from_environment` (defaults behave as before) plus a Go-only
  shape check with actionable errors; a missing key *value* is a labelled
  pass, never an error. An unrecognized provider name stays a startup
  error at run time.

## Validation limits (R5)

Offline checks cannot establish key validity, endpoint reachability, model
existence/permission, quota/429 behavior, or live model quality. No live
paid testing exists; mocked success never claims real model quality.

## Resume constraint (R4)

A run records a secret-free connection fingerprint alongside the component
binding. Both assembly paths capture this public property from the actual
LLM client before metering wraps it. The fingerprint contains endpoint,
protocol, model, credential variable name and User-Agent, with canonical
JSON hashing; it excludes the key value and session identifier. Ordinary,
cohort and checkpointless recovery compare it before rebuilding transports
or reconciling work. An absent fingerprint is omitted from serialization
to preserve legacy hashes.

## Consequences

- Nothing outside `redteam.llm` (adapter) and the selection/CLI
  surface changes when Go models change: new model ids need no code change.
- Old checkpoints keep their hashes; `litellm` behavior is unchanged.
