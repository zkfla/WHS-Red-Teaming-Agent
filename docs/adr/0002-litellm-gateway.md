# ADR 0002: LiteLLM Behind an LLMClient Port

## Status

Accepted, with a known compatibility caveat.

## Context

We want to use LiteLLM as an LLM provider abstraction, but the brief
(instruction.md section 13) is explicit that LiteLLM call sites must
not spread across Agent code — otherwise swapping providers or gateway
libraries later means touching every agent.

## Decision

Define `redteam.core.ports.llm.LLMClient` (`complete`,
`complete_structured`) as the only interface Agent/Orchestration code
depends on. Two implementations:

- `redteam.llm.fake.FakeLLMClient` — deterministic, no network, used
  by default in the current stub flow and in tests.
- `redteam.llm.litellm.LiteLLMClient` — wraps `litellm.acompletion`.

## Compatibility caveat

`litellm` (as of this writing) requires Python 3.11+
(`typing.NotRequired`, used internally) and fails to import on the
3.10.4 that's actually available in local dev right now. To avoid
breaking the base install:

- `litellm` is an optional dependency (`pip install -e ".[llm]"`), not
  a hard requirement.
- `redteam.llm.litellm` imports `litellm` lazily, inside each method
  body — the module itself, and everything that imports
  `LLMClient`/`FakeLLMClient`, stays importable on 3.10.

## Consequences

- Nothing outside `redteam.llm` needs to change when/if the team moves
  to a different LLM gateway (or drops LiteLLM) — implement
  `LLMClient` and rewire `redteam.app.bootstrap`.
- Until the team is on Python 3.11+, `LiteLLMClient` can be
  constructed but its methods will raise `ImportError` when actually
  called. Tests and the current vertical slice use `FakeLLMClient`, so
  this doesn't block anything at this stage.
- Revisit once the team standardizes on 3.11+: bump
  `pyproject.toml`'s `requires-python`, fold `llm` back into the base
  dependency set if desired.
