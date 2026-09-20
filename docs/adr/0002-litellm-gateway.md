# ADR 0002: LiteLLM Behind an LLMClient Port

## Status

Accepted. The Python-version compatibility caveat below no longer
applies — current `litellm` releases install and import fine on this
project's 3.10+ floor (verified directly: `pip install -e ".[llm]"` then
`import litellm` on 3.10.4). Kept as an optional extra regardless, for
its dependency footprint (see "Compatibility caveat" below, updated).

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
- `redteam.llm.litellm.LiteLLMClient` — wraps `litellm.acompletion` for
  every provider except the `chatgpt/<model>` subscription path (ChatGPT
  Plus/Pro via OAuth device flow, no API key), which calls
  `litellm.aresponses()` directly instead — verified directly that
  bridging that provider through `acompletion()` is unreliable for a
  short, non-agentic prompt (see the module docstring on
  `redteam.llm.litellm` for the exact failure and why). Provider
  identity is resolved via litellm's own `litellm.get_llm_provider(model)`,
  not a hand-maintained `"chatgpt/"` prefix check, so a future litellm
  provider rename doesn't silently misroute.

## Compatibility caveat (superseded)

An earlier `litellm` release genuinely required Python 3.11+
(`typing.NotRequired`, used internally) and failed to import on the
3.10.4 available in local dev at the time. That is no longer true:
current releases satisfying this project's `litellm>=1.50` pin install
and import fine on 3.10 (verified directly, not assumed). `litellm`
stays an optional dependency regardless — not to work around a Python
version, but because it pulls a large transitive dependency footprint
(`boto3`, `openai`, `tiktoken`, `tokenizers`, ...) the base install
shouldn't force on everyone:

- `litellm` is an optional dependency (`pip install -e ".[llm]"`), not
  a hard requirement.
- `redteam.llm.litellm` imports `litellm` lazily, inside each method
  body — the module itself, and everything that imports
  `LLMClient`/`FakeLLMClient`, stays importable without it.

## Consequences

- Nothing outside `redteam.llm` needs to change when/if the team moves
  to a different LLM gateway (or drops LiteLLM) — implement
  `LLMClient` and rewire `redteam.app.bootstrap`.
- Without the `llm` extra installed, `LiteLLMClient` can be constructed
  but its methods raise `ImportError` when actually called. Tests and
  the default vertical slice use `FakeLLMClient`, so this doesn't block
  anything at this stage.
- `requires-python` (3.10) is unrelated to this ADR now — nothing here
  blocks on a Python version bump.
- litellm caches a persistent async HTTP client per provider for
  connection reuse (`litellm.in_memory_llm_clients_cache`); a short-lived
  `redteam` CLI process (one `asyncio.run()`, then exit) can otherwise hit
  a noisy-but-harmless `Fatal error on SSL transport ... Event loop is
  closed` at shutdown, since litellm's own `atexit` cleanup closes those
  clients on a *new* event loop created after the original one is already
  gone. `redteam.app.cli.__init__._dispatch` explicitly calls
  `litellm.close_litellm_async_clients()` on the still-live loop right
  after the command handler returns (only if `"litellm"` is already in
  `sys.modules`, so this never forces the import for `FakeLLMClient`-only
  installs) — verified directly that this removes the traceback.
