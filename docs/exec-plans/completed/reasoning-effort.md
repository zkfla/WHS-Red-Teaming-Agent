# Runtime reasoning effort and web settings

## Status

COMPLETED on original checkout, dev (base 02d9e896), 2026-10-13. Root AGENTS and module instructions apply. Existing user .gitignore and untracked materials are preserved.

## Goal and requirements

Expose optional reasoning effort through the shared LLM configuration, environment, interactive setup and web settings. Deliver the selected value to real OpenCode Go/LiteLLM requests, including structured planning and native worker calls. Preserve unset request behavior and old identities. Persist explicit effort in existing connection/run identity and reject changed effort on resume. Web changes must validate atomically, survive saved settings reload, and permit clearing to provider default. Support OpenCode in the existing web provider selection because its current litellm-only validation prevents this user flow.

## Design and boundaries

Use one public configuration vocabulary and existing SDK/provider mapping; no model-name allowlist, silent effort downgrade, new state store, or new orchestration branch. API capability errors remain explicit. SDK metadata is incomplete for custom Go model IDs: verify protocol support and actual wire mapping rather than claiming universal model support. Unset effort is omitted from serialization/fingerprints to preserve existing runs. No live API or target calls are required.

## Tasks

- A (Muse 1.3 High): core configuration/environment, LLM effort helper and API mapping, bootstrap, connection identity, offline request/config regression tests.
- B (Muse 1.3 High): web settings backend and existing static settings screen; provider selection, effort apply/summary/persist/reload/clear and regression tests. Consume A's public contract.
- Root: interactive setup, environment example/docs, integration/resume review and tests, final validation.
- Orchestrator (DeepSeek 4.1 High): coordinate disjoint workers; Opus 4.8 High and AGY Gemini 3.8 Flash High independent review.

## Acceptance and verification

Check environment parsing and invalid values; exact chat/responses/messages wire forwarding; structured output retains effort; unset fingerprints unchanged; effort changes alter binding and reject resume; web provider/effort save, reload and clear; no secret echo; interactive configuration. Run relevant pytest, Ruff, mypy and browser rendering/interaction verification. Record mock/offline and live execution separately. Commits/push only within prior user-authorized scope; no force push.

## Progress and decisions

Initial inspection: LLMConfig has no effort; LiteLLMClient supports default kwargs. Go structured Messages requests currently overwrite output_config, which must preserve effort. Web SettingsStore currently fixes provider to litellm and forces it on model edits, blocking Go selections. Existing Go connection fingerprint and central original-binding gate can carry explicit effort; generic LiteLLM requires equivalent optional identity without changing unset behavior.

User clarified multi-provider support: remove the web litellm-only blocker, while keeping the existing two adapter choices and using LiteLLM provider-prefixed models for downstream providers. Do not turn unsupported settings into silently reduced effort.

Root and reviewers found a necessary native continuation gap: NativeCompletion drops signed thinking blocks before the next tool result. Anthropic's current primary [thinking documentation](https://platform.claude.com/docs/en/build-with-claude/thinking) specifies `display: omitted`: empty thinking text plus encrypted signature, replayed unchanged. A bounded optional opaque encrypted continuation on NativeCompletion and existing checkpointed assistant history addresses this without storing plaintext chain-of-thought or adding a cache/store. Muse C owns core/ports/llm.py and native react echo/tests; A owns provider capture/replay and request omission mode. Plaintext reasoning is never converted into a forged empty signed block. Unexpected plaintext or malformed continuation must produce a clear failure.

Validation in progress: CLI effort setup 4 PASS; adjacent environment/CLI 10 PASS. Web backend/static/frontend 81 PASS with one historical Windows POSIX-mode assertion failure (will verify on Linux). Real browser against isolated local SettingsStore confirms OpenCode+high save/reopen and return-to-default clearing. UI-only fixture never starts an investigation or calls a provider. Added fields exposed a clipped dialog header at 720px; repair delegated to web owner.

## Review and validation log

- Root found resume preflight dispatch missing the LiteLLM explicit-effort binding; repaired through a shared provider dispatch helper. Per-call effort changes now reject a mismatch with the configured client; explicit drop_params cannot silently drop effort.
- Root found explicit key references equal to provider defaults were misclassified as derived when switching providers. Web owner added provenance and a regression verifying an explicit LLM_API_KEY remains selected for OpenCode.
- Opus proposed an adapter memory cache because it assumed plaintext reasoning. Rejected: current Claude omitted-thinking protocol returns empty public thinking plus encrypted signature, and a memory cache loses continuity across resume. Keep bounded encrypted opaque continuation on existing assistant checkpoints. Plaintext is never saved or rewritten into signed empty blocks.
- AGY Gemini 3.8 Flash High completed after its first 8-minute job timed out and continuation recovered the review. Five reported issues were weighed: unchanged provider payload preserves current provider; overridden filters identical values; normal CLI startup loads existing effort and therefore does not offer a conflicting blank prompt; global drop_params rejection prevents the SDK's global OR behavior; Windows POSIX mode assertion predates this change. No new blocker accepted from those findings.
- Linux Python 3.10 with installed LiteLLM 1.104.0: relevant offline regressions 346 PASS (125.04 seconds), including native ports, request scopes, connection/resume, web and CLI. Separate targeted resume/config/CLI run: 52 PASS.
- Full Ruff PASS; Linux fresh-cache mypy PASS across 771 source files. Existing mounted .mypy_cache is corrupt (sqlite database image malformed); fresh /tmp cache avoids it without deleting existing files. Windows-only type noise is not counted as a Linux result.
- Browser 1280x720: repaired header visible; dialog scrolls; generic API + anthropic/fixture-model + high saves/reopens. OpenCode save/reopen and clearing were checked earlier. This is an isolated fixture, not a provider acceptance test.
- Phase commits: 24ac464d config/environment/CLI; 53471d42 web provider/effort/key-reference preservation. Adapter capture/replay remains pending; full offline regression suite is running.

## Final implementation and repair

The stalled Muse adapter worker was interrupted without substituting another model. The selected DeepSeek orchestrator implemented encrypted capture/replay; root completed SDK-derived omitted-thinking configuration and exact HTTP verification. The selected Opus reviewer reread the final source and accepted the encrypted-only checkpoint contract, superseding its earlier cache recommendation. No must-fix findings remained.

SDK mapping exposed a concrete blocker for custom Messages model IDs: the SDK catalog rejected reasoning_effort/thinking before HTTP despite the provider mapper being able to construct them. The adapter now derives thinking mode/budget through ProviderConfigManager and explicitly forwards only those mapped protocol fields using the SDK's allowed_openai_params. It adds omitted display without inventing an adaptive mode or token budget. Unsupported models still fail explicitly at their API. OpenCode output_config.effort merges on all Messages calls, with format retained for structured calls.

Capture and replay validate encrypted-only blocks. Thinking blocks require exactly empty thinking plus a signature; redacted blocks require encrypted data. Responses projection retains only encrypted content, id and empty summary, requests encrypted content through include union, and handles the completed streaming output. Replay rejects plaintext or tampered continuation. Gemini's SDK signed call IDs remain byte-identical through the existing tool-call contract; no Google plaintext thoughts were added. SQLite close/reopen verifies the assistant continuation remains unchanged.

Root repaired first-startup Go settings so the runtime receives the same derived OPENCODE_API_KEY reference displayed by the web summary. Explicit references remain authoritative, and derived references switch with providers. SDK-optional tests skip only real-SDK checks when the optional dependency is absent; the dedicated SDK CI job now explicitly runs effort and continuation tests with its required SDK preflight.

## Final validation log

| Check | Result | Scope and limitations |
|---|---|---|
| Linux Python 3.10 broad regression | 4079 PASS, 64 SKIP, 1093.76 s | Started during integration; optional/live/container conditions skipped. Final modified paths were checked again below. |
| Final native/effort subset, Python 3.10 | 153 PASS | Includes strict plaintext rejection, encrypted projection/replay and streaming; offline SDK/stub fixtures. |
| Final web/config/CLI/scopes/resume subset | 176 PASS | Includes first-startup reference publication and changed/cleared effort refusal. |
| Final adapter/resume subset, Linux Python 3.14 | 173 PASS | Installed LiteLLM 1.104.0; network boundary intercepted. |
| Final adapter/resume subset, Windows Python 3.14 | 173 PASS | Installed LiteLLM 1.104.0; network boundary intercepted. |
| Exact final SDK HTTP effort bodies | 4 PASS | Chat effort; Responses effort/encrypted replay; Messages adaptive and SDK-budgeted enabled omitted thinking. |
| Native continuation + actual SQLite reopen | 13 PASS | Exact signature survives closed/reopened SQLite checkpoint. |
| Full Ruff | PASS | src, tests, examples, two existing container servers. |
| Full mypy | PASS, 771 files | Linux Python 3.10, isolated fresh cache; existing corrupt cache untouched. |
| Package build | PASS | Wheel and source archive written outside checkout. |
| Browser | PASS | Isolated UI fixture, high save/reopen, provider switching, clear/default, 720px dialog. No investigation launched. |

One SDK/Pydantic ReadOnly warning was emitted in installed-SDK runs. The pre-existing Windows web POSIX file-mode assertion was verified on Linux without weakening it. No live model API, MCP target scan, paid provider acceptance or model-quality check was performed. Supported levels remain provider/model dependent.

## Requirement traceability

| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| FR-001 optional effort + default compatibility | core/contracts/llm_config.py, core/config.py, core/environment.py, CLI | CLI/config and legacy identity tests | PASS |
| FR-002 multi-provider delivery | llm/reasoning_effort.py, litellm.py, opencode_go.py, bootstrap.py | Exact HTTP bodies for Chat/Responses/Messages | PASS |
| FR-003 web setting persistence/clear/provider switching | web/settings.py and static settings dialog | Backend tests + browser save/reopen/clear | PASS |
| FR-004 resume consistency | explicit-effort connection identity and existing original-binding gate | high-to-low, set/unset refusal; same binding/key rotation | PASS |
| FR-005 native reasoning continuity without plaintext | NativeCompletion.continuation, react echo, adapter projection/replay | Rejection/streaming/replay and SQLite reopen | PASS |
| NFR-001 existing module boundaries/single vocabulary | shared core vocabulary, adapter-owned provider meaning | Root + final Opus review, mypy/Ruff | PASS |
| C-001 preserve user changes and avoid live calls | original checkout, explicit file staging, isolated wire/UI fixtures | User dirty files excluded from commits | PASS |

## Completion criteria and commits

Required functionality is verified, accepted review blockers are resolved, and no live acceptance claim is made. Phase commits: 24ac464d configuration/CLI; 53471d42 web; 25d7fb82 checkpoint continuation; a6ce8ff2 provider effort/encrypted replay/CI tests; be003df1 first-startup web reference repair. Documentation is committed separately after final validation. Normal push targets origin/dev; no force push or branch switch.
