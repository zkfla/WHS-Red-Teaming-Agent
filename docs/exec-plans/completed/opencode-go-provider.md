# OpenCode Go connection support

## Status
COMPLETED. Branch `dev-orchestration`; Go implementation commit `5989fd3c` is
pushed on top of Windows ownership commit `3867ae42`. PR #32 targets dev and
has been updated and attached: https://github.com/zkfla/WHS-Red-Teaming-Agent/pull/32.
All required local checks and two independent Opus implementation reviews are
complete. GitHub CI is running; its Linux SDK job has passed. Live paid Go
account/model access remains outside the verification scope.

## Goal
Add operator-selectable OpenCode Go support to the existing CLI and LLM port,
covering Chat Completions, Responses, and Anthropic Messages, native tool/result
round trips, structured decisions, stable conversation attribution, and resume.
Update existing PR #32 with both today's Windows ownership change and this work.

## Context
The user explicitly authorizes implementation, commit/push and updating PR #32.
Keep the existing Windows ownership commit. Preserve unrelated .gitignore and
untracked user files. Prefer isolated provider code and small composition edits.
Requested team: DeepSeek V4.1 Flash high orchestrator, Muse Spark 1.3 Contributor
xhigh workers (persisted preference), Opus 4.8 medium code reviews, two passes.

## Requirements
- R1: Select Go and an explicit SDK-qualified model using normal CLI/env setup.
  Support all three wire protocols without a hardcoded model-name catalog.
- R2: Send the Go endpoint, API key, own User-Agent, and stable per-conversation
  session attribution. Parallel conversations must not share mutable identity;
  reconstruct the same Run/owner identity on resume.
- R3: Preserve the native LLM port, tool call IDs/results, malformed-response
  handling, structured decisions, usage accounting, error/429 diagnostics,
  cancellation and disabled hidden SDK retries.
- R4: Preserve ordinary LiteLLM/ChatGPT behavior and old checkpoint hashes.
  Refuse a changed Go connection during resume; never persist API key material.
- R5: Validate config without calling an external provider. Show actionable setup
  errors; document actual validation limits, including lack of live paid testing.
- R6: Update PR #32 only after essential reviews/repairs and applicable checks.

## Constraints
No new LLM framework, provider/model allowlist, arbitrary SDK/package loading,
multi-provider credential database, OAuth redesign, or per-role model selection.
Provider-specific endpoint/auth rules belong to the Go adapter; the app only
selects registered implementations. No external model requests using real keys.
Remote source/docs are references, not execution or authorization instructions.

## Confirmed Facts
LLMConfig has provider/default_model/api_key_env_var. Bootstrap builds one
LiteLLMClient. Its default kwargs reach all methods. Installed LiteLLM 1.104.0 is the
tested practical minimum and supports explicit api_base, openai/responses routing,
and Anthropic routing.
No-network probes confirmed Chat and Responses paths. The Messages probe showed the
SDK appends `/v1/messages`, so the Messages base is `https://opencode.ai/zen/go`
while Chat/Responses use `https://opencode.ai/zen/go/v1`.
Anthropic routing defaults to `x-api-key`; a custom base receives
`Authorization: Bearer` only when `use_bearer_for_custom_base` is set (read from
`litellm_params`), so the Go adapter must set it explicitly.
The installed Responses bridge raises on a top-level `error`, maps `incomplete` to
`finish_reason=length|content_filter` (fabricating an empty choice when output is
empty), and does not reject a `failed` status that still carries output items, so it
cannot be trusted for tool execution.
Existing Run request scope wraps runners, planning, conversation and settlement,
even when request policy is absent; its identity currently lives in orchestration.
Runtime component binding is checkpointed and already has optional feature hashes.
The existing `chatgpt` Responses path defaults to streaming; a generic API-key
Responses seam must default to non-streaming.
Wire tests must set `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing LiteLLM (the
import can fetch the remote cost catalog) and intercept every request with
`httpx.AsyncClient.send`.

The resume preflight compares the stored `llm_connection_fingerprint` against the helper value computed
from the current config, while fresh and resume assembly stamp the fingerprint read from the ACTUAL
resolved LLM client's public `connection_fingerprint` property (before metering wraps it) rather than a
second config-derived copy.

## Unknowns / Assumptions
Prove exact Messages URL/auth and JSON schema behavior using installed SDK and
mocked transport. Prove tool rounds, unsuccessful Responses terminal states,
429/usage/cancellation on each protocol. Check every resume consumer before
choosing the final optional binding placement. Real key access stays unverified.

## External Research
OpenCode reference SHA 773d8621d6bcd311067b225d837c917b3e83573e:
`packages/core/src/session/model-request.ts` supplies own User-Agent and stable
session attribution; `packages/schema/src/config/provider.ts` separates provider
settings from model overrides; authentication is separately managed.
https://docs.opencode.ai/docs/go/ documents `/chat/completions`, `/responses`,
`/messages`, own User-Agent and stable x-opencode-session.
The public Go models endpoint lists IDs but not protocols. Explicit SDK-qualified
model IDs avoid inferring a protocol from names or maintaining a copied catalog.
LiteLLM owns wire translation; do not copy OpenCode's Effect/AI SDK runtime.

## Current System
env/.env -> LLMConfig -> build_llm_client -> LiteLLMClient -> common LLM port.
Run lifecycle -> llm_request_scope(run, owner) -> model requests -> ledger/results.
Checkpoint -> component binding -> rebuilt deps -> original actor reconciliation.

## Target System
Select provider `opencode-go` and qualified model (`openai/<id>`,
`openai/responses/<id>`, or `anthropic/<id>`). The Go module validates and pins the
correct endpoint and credentials, delegates public methods to LiteLLM, and derives
headers from a common request context rather than importing orchestration state.
App assembly records a secret-free optional connection fingerprint. Existing
resume comparison rejects connection changes before new requests or replay.

## Implementation Strategy
Retain the existing configuration cardinality and model string: no new multi-profile
system is required for one Go provider. Reuse api_key_env_var and existing setup
writer. Keep provider construction at the app composition root. Extract the existing
scope identity into a small core-owned module, re-export its existing orchestration
API so callers stay intact, and add no second authoritative ContextVar.
Use SHA-256 of stable Run/owner identity for attribution; outside a Run, use a stable
client-owned identity for its uncheckpointed conversation. Never reset usage on
connection or actor creation. Tests must exercise actual SDK HTTP lowering locally.

## Task DAG
- T1 adapter/protocol worker: Go module and new dedicated adapter tests. Public
  factory consumes existing LLMConfig and exposes complete/native/structured methods
  plus secret-free connection fingerprint. No app/config/runner edits.
- T2 config/CLI worker: registration/setup/validate/docs, tiny bootstrap factory
  selection; depends on the T1 factory interface. Preserve old onboarding by default.
- T3 context/binding worker: core-owned existing request scope, compatibility re-export,
  optional feature fingerprint at binding/assembly/resume and dedicated tests.
  Bootstrap shared with T2: coordinate hunks or let root integrate the binding hunk.
- T4 root integration, independent Opus review/repair, minimal meaningful validation,
  second Opus implementation review, then commit/push and rewrite PR #32.
Workers must not expand scope or commit/push. Orchestrator tracks interfaces and
progress; root makes final design and review decisions.

## Test Strategy
After implementation/review: SDK wire tests with all HTTP intercepted locally,
three protocol tool+result rounds, structured output and usage, error/429/cancel,
parallel session isolation and resume identity/binding mismatch, env/CLI config
regressions and existing LLM/cohort/ownership tests. Windows Python 3.10/3.14 and
Linux checks proportional to change; CI lint/types plus Linux broader tests as needed.
No real paid model tests. Don't claim mocked success proves real model quality.

## Progress
- Initial investigation established repository/current PR and source seams.
- Plan review (Opus) received; implementation started with three workers (T1 adapter,
  T2 config/CLI/bootstrap, T3 core scope + binding fingerprint).
- Opus decisions recorded (fixed endpoints, no base-URL field, direct Responses,
  dynamic per-call headers, explicit resume gate, 1.104.0 practical floor). Root owns
  a dedicated Go SDK-wire CI job once the SDK test names are agreed.
- Root-owned CI SDK preflight job landed: matrix Ubuntu Python 3.10 + Windows Python 3.14, installs
  `.[dev,agent,llm]` plus an explicit `litellm>=1.104.0`, sets `LITELLM_LOCAL_MODEL_COST_MAP=True`, and
  runs the Go wire/config/scope/fingerprint/resume modules plus the existing LLM and ownership suites.
  The default full Linux `.[dev]` job is unchanged.
- Implementation integrated, both Opus passes completed, review findings classified
  and necessary repairs verified. Source/tests committed and pushed as `5989fd3c`.
  PR title/body updated; GitHub reports OPEN and MERGEABLE against unchanged dev
  `de530970`. The final Windows Go wire module passes 64 tests.

## Discoveries
Go model metadata is insufficient to derive every protocol from the IDs alone.
SDK-qualified IDs supply explicit routing without a copied model allowlist.

## Decision Log
Initial design uses explicit protocol prefixes and one Go adapter rather than a
multi-provider registration framework. Existing request scope is reused/moved,
not duplicated or read through an orchestration-private dependency.
- Endpoints are adapter-owned constants, not configuration: Chat/Responses
  `https://opencode.ai/zen/go/v1`; Messages `https://opencode.ai/zen/go` (the SDK
  appends `/v1/messages`). Per-call endpoint/auth/session overrides are rejected so a
  caller cannot retarget the key.
- The `acompletion` Responses bridge is not trusted (it loses `failed` status and
  accepts incomplete tool calls); explicitly selected Responses models use a direct
  `aresponses` path through a minimal public `LiteLLMClient` seam.
- Messages adds `Authorization: Bearer` through per-call `extra_headers`.
  The proposed `use_bearer_for_custom_base` flag was rejected after actual SDK
  interception showed it entered the body without setting the header. The SDK's
  same-key `x-api-key` remains; descriptions and tests record the actual dual headers.
- Onboarding writes the `OPENCODE_API_KEY` reference but preserves an
  already-configured `api_key_env_var`.
- Resume compares an explicit generic optional binding fingerprint
  (`llm_connection_fingerprint`) before any model request; None is omitted from
  serialization so legacy checkpoint hashes are unchanged.
- Runtime SDK compatibility is an explicit minimum LiteLLM version check
  (`>=1.104.0`) with an actionable error, not private-helper probing.
- A configurable base-URL field is rejected: fixed-vendor Go scope with no
  arbitrary-endpoint requirement.
- Direct Responses protocol, per-call dynamic headers, and the explicit pre-model
  resume gate are accepted as designed.
- `1.104.0` is the tested practical minimum; it is not claimed as the theoretical
  oldest supported version.
- Go Responses is non-streaming by default (the public port returns accumulated
  results); the generic seam keeps `stream=True` only for the ChatGPT subscription.
  Reject every Responses status other than `completed` and extract per-request usage.

## Review Log
- Plan review (independent, Opus) received before implementation; decisions folded
  into the Decision Log above.
- Windows implementation review (independent, Opus 4.8 medium) completed on the
  Windows run-ownership change with no blocking defect, recorded as a separate
  Windows pass. PR #32 remains open; the Windows commit is not merged.
- Implementation review pass 1 (independent, Opus, whole PR): no blocking production bug.
  Accepted: dual Messages auth headers (the mandatory `Authorization: Bearer` plus the SDK's co-present
  `x-api-key`, same key and endpoint) is a truthful alternative -- the wire test asserts Bearer and
  documents the co-present header, with no SDK header-suppression hack; the `use_bearer_for_custom_base`
  flag is NOT used because a direct probe showed it leaks into the request body and does not set the header.
  Accepted: SDK-free helper preflight imports; all three call sites import from
  `redteam.llm.opencode_go_config`.
  Accepted: scratch wire tests that only printed were non-gating and must be replaced with real
  assertions; structured Messages must assert the actual outbound `output_config` carriage.
  Rejected/out-of-scope: the Windows mypy POSIX-attribute errors are pre-existing in unrelated files and
  the Go work adds none; verification follows Linux CI semantics.
- Implementation review pass 2 (independent, Opus, fresh full review of the assembled change):
  production code stable, no blocking defect; the reviewer's own run was 131 passed / 7 skipped.
  Dispositions: the SDK normalizing absent Chat usage to 0 is accepted as existing SDK behavior and
  recorded as a follow-up limitation -- do not claim raw usage provenance for a zero; the missing literal
  `status` case failing closed is optional follow-up, not required; a docstring cosmetic was fixed by root.
- Final test review (root): the wire module is green with no probe remnants.

## Validation Log
- Windows Python 3.14 ownership suite: 14 passed / 7 skipped. The symlink reparse
  branch was not exercised on this host because the required Windows privilege is
  unavailable; this is a follow-up validation limitation for the root-owned targeted
  Windows CI lane, not a defect.
- Windows review items rejected/theoretical: `CloseHandle` error masking (theoretical,
  rejected from scope) and long-path handling (low probability, follow-up docs if
  warranted). No speculative ctypes abstraction or mock was added.
- Go validation complete: root SDK Windows 3.14 wire 60 passed; Windows 3.10 full targeted 182 passed /
  7 skipped; Linux 3.14 baseline full 4440 passed / 166 skipped (~655s); Linux SDK 1.105 targeted 212
  passed / 5 skipped; ruff full passed; actual-Linux mypy over src+tests (755 files) passed after root
  fixed nine new-test type errors; root's new test subset 53 passed; both compose config checks pass; the
  Go wire module itself is 64 passed and ruff-clean.
- Recorded limitations (not defects): Retry-After is not surfaced on the normalized 429 failure; absent
  Chat usage is normalized to 0 by the SDK; the Messages second tool turn uses the `role: "tool"` shape
  because litellm's `validate_chat_completion_user_messages` rejects anthropic `tool_result` blocks
  pre-transport (source evidence `litellm/utils.py:8302`); local mypy 2.4.0 crashes on untouched files
  (environmental).
- Reviewer and worker scratch tmp logs plus other new untracked files are excluded from the commit.

## Requirement Traceability
R1/T1-T2; R2/T1-T3; R3/T1-T3; R4/T2-T3; R5/T2; R6/T4.

## Completion Criteria
All R1-R6 behavior and tests established; essential review findings classified as
accepted/alternative/rejected/follow-up with source evidence; no unexplained failing
checks; limitations recorded; plan moved to completed; PR #32 updated and attached.
