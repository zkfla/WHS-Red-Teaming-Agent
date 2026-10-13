# Configuration

Schema: `redteam.core.config`. So teams don't each invent their own
`.env`/config shape (instruction.md section 19).

## EngagementConfig

File-loadable shape of an `Engagement` — same fields minus
`engagement_id`/`created_at`, assigned at load time:

```python
config = load_engagement_config("examples/engagement.yaml")
engagement = config.to_engagement(engagement_id=EngagementId(new_id("eng")))
```

This is what `redteam.app.bootstrap.load_engagement` actually uses
today (`examples/engagement.yaml`).

## AppConfig

Wraps `EngagementConfig` alongside per-area config:

```python
class AppConfig(BaseModel):
    engagement: EngagementConfig
    llm: LLMConfig
    orchestration: OrchestrationConfig
    capability: CapabilityConfig
    logging: LoggingConfig
```

See `examples/app.yaml` for the full shape. **Current status**: fully
wired, via a second, `AppConfig`-aware entry point —
`redteam.app.bootstrap.build_application(config, ...)` — that reads
every section (`LLMConfig.provider` -> `LiteLLMClient`/`FakeLLMClient`,
`CapabilityConfig.mcp_servers` -> real MCP server wiring,
`LoggingConfig` -> `logging.basicConfig`) and is what the CLI (`redteam
run`, and the interactive `redteam` entrypoint) always uses.

There is no application-config YAML file or `--config` flag in the CLI:
`redteam.app.cli._shared.load_app_config_for_run(engagement_path)` builds
the `AppConfig` the CLI passes to `build_application` from
`engagement.yaml` plus the process environment only
(`LLMConfig.from_environment()`, `OrchestrationConfig.from_environment()`,
`mcp_server_ids_from_environment()`) —
see `.env.example`. `redteam.core.config.load_app_config(path)` (the
YAML-file loader `AppConfig.model_validate` shape above describes) still
exists as a general library capability for code that calls
`build_application()` directly, outside the CLI (e.g.
`examples/finance_agent_demo.py`, `deploy/mcp/finance/app.yaml`) — the
CLI itself never reaches it.

The CLI accepts these optional orchestration environment variables:

| Variable | Default | Validation |
|---|---|---|
| `REDTEAM_ORCHESTRATION_MAX_STEPS` | `50` | integer >= 1 |
| `REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION` | `true` | only `true` is supported |

Interactive, `run`, and `resume` share this loader. `--max-steps` takes
precedence over the environment value. Library callers can still supply an
explicit `OrchestrationConfig` without reading the environment.

New API/YAML inputs containing `max_retries` or
`retry_retryable_tool_failures`, and the environment variable
`REDTEAM_ORCHESTRATION_MAX_RETRIES`, are rejected as removed settings. A
retryable tool failure is returned to the model for a follow-up decision.
Original saved Run settings decode these fields through
`OrchestrationSettings` as passive binding identity; they never enable retries.

`run_engagement(orchestration_config=...)` rejects a supplied override before
entering the Run. Configure a new Run through `build_application(AppConfig, ...)`
or its `orchestration_settings` assembly argument. Execution keeps the assembled
dependencies and binding; resuming a Run does not grant a new budget.

An interactive session reloads and validates its engagement file before each
new Mission. Invalid YAML, missing files, or invalid scope/ROE prevent that
Mission from starting, while the session stays available for a corrected file.
Already running Missions retain their authorized runtime snapshot; changes
during a run continue to use the existing Operator Intent/Control boundaries.

`build_runtime_deps(engagement, ...)` — `Engagement`-only, no `AppConfig`
— is a **test/example-only** composition helper, not a second production
entry point: it exists for callers that only have a bare `Engagement`
and want the minimal wiring to run one (`examples/minimal_run.py`, most
unit/integration tests). Production code always goes through
`build_application(AppConfig, ...)`.

## Web investigation request permissions

The investigation form separates path scope (`site`/`paths`) from request
permissions (`read_only`/`custom`/`full`). Full permits all supported HTTP methods,
valid query names and ordinary request headers within the selected origins and
paths. Host, authentication/cookie and transport-control headers remain provider
owned. Custom selects methods, query names, headers and JSON/form body permission.
Scope, ROE, approval and execution budgets still apply to every action.

`metadata.web_policy` records these explicit operator selections. Generated
`DeploymentSpec.request_permissions=testing` enables the write-method declaration;
historical specs retain the read-only default. New schema defaults are omitted
from serialized historical policies to preserve their digests. Edits generate
new bound artifacts and never overwrite old Run policies. Existing Run snapshots
and resume bindings are unchanged by editing today's investigation.

An optional response preview exposes at most 16,384 characters of redacted text
or JSON as untrusted data. The raw response digest and size remain available;
binary content stays omitted. JSON and form requests are supported; multipart
uploads and arbitrary raw bodies are not advertised as available.

Sessions use named principal/session references and either a bearer environment
variable name or an existing private Playwright storage-state file, plus explicit
paths. The form never accepts raw credential values or claims a login was verified.

Kali communication is independently closed, inherited, or selected via an existing
reviewed gated-network policy file. Selected policies require a pinned sidecar,
bounded network budgets, registry names matching the chosen hosts, and no package
mirror/install route. Startup checks each registry IP against its selected host's
resolved addresses and snapshots the hash-bound file privately. This selection
does not build a sidecar or prove its kernel prerequisites; those remain execution
startup/runtime checks of the existing gate. Local shell execution stays network
closed. Full web request permission never implies unrestricted shell networking.

Conversation inputs receive configured, unverified server declarations before
a Run and finalized catalog/health data while attached to a live Run. Declaration
inspection performs no MCP or Docker startup. Planner and worker contexts receive
the generated target permissions; tool discovery does not replace Control admission.

## Secrets

Never stored inline. `LLMConfig.api_key_env_var` names an environment
variable; `LLMConfig.resolve_api_key()` reads it on demand:

```python
config = LLMConfig(api_key_env_var="LLM_API_KEY")
api_key = config.resolve_api_key()  # os.environ.get("LLM_API_KEY")
```

`.env.example` documents the variables teams are expected to set.
`redteam` (every subcommand) loads a `.env` file from the current
directory into its own process environment automatically at startup,
best-effort (`redteam.app.cli._shared.load_dotenv_if_present`) — a real
shell/CI-exported variable always wins over a `.env` value, so this only
fills in what is otherwise unset.

## CLI Docker workroom preparation

Investigation startup enables automatic Kali preparation by default.
`REDTEAM_SHELL_AUTO_SETUP=false` preserves manual setup. An explicit
`REDTEAM_SHELL_POLICY_PATH` takes precedence over the managed default.
Preparation starts installed Docker Desktop on Windows when needed, waits
for a Linux engine, builds/reuses an immutable image, and generates its
matching program manifest. Missing Docker produces installation guidance.
Read-only validation and tool inspection do not provision Docker.

Prepared runs record `execution_environment_fingerprint` in their existing
component binding. Resume restores that exact provider-owned cache record
and rejects missing images, changed Docker endpoints or changed explicit
policies. Legacy bindings omit the field and keep manual resume behavior.
The optional policy `docker_context` pins workroom commands to the same
Docker context used for preparation. Existing context-free manual policies
retain their ambient Docker selection.

Central Runs with a pinned LLM connection or execution environment also keep
their component binding in the existing `run_configuration` record. Central
resume and original collection read this identity before provider assembly;
changed Go routing is refused and the original prepared Docker fingerprint is
forwarded to preparation. Historical records without these optional identities
retain their original metadata and manual shell selection.

See [workroom deployment](../../deploy/docker/shell-workroom/README.md)
for first-use requirements and isolation constraints.

## LLM providers: `litellm` and `opencode-go`

`LLMConfig` selects `provider`, `default_model`, `api_key_env_var` and optional
`reasoning_effort` from `REDTEAM_LLM_PROVIDER` / `REDTEAM_LLM_MODEL` /
`REDTEAM_LLM_API_KEY_ENV_VAR` / `REDTEAM_LLM_REASONING_EFFORT`.
The model prefix selects a downstream provider through LiteLLM; OpenCode Go
continues to use its existing protocol selection. No per-model allowlist is
introduced.

- `REDTEAM_LLM_PROVIDER=opencode-go` with an explicit SDK-qualified model:
  `REDTEAM_LLM_MODEL=openai/<id>`, `openai/responses/<id>`, or
  `anthropic/<id>`. The qualified model selects the wire protocol; there is
  no model-name catalog. Endpoints are fixed adapter-owned constants -- no
  endpoint field exists anywhere in config.
- Key reference: onboarding writes `OPENCODE_API_KEY` and persists
  `REDTEAM_LLM_API_KEY_ENV_VAR=OPENCODE_API_KEY`, but an already-configured
  `REDTEAM_LLM_API_KEY_ENV_VAR` is always preserved, never overwritten.
  The key value itself is never stored inline, logged, or checkpointed.
- Dependency floor: the `llm`/`agent` extras stay at `litellm>=1.50` (no new
  extra, no pyproject change). The Go adapter checks at runtime that the
  installed `litellm` is `>=1.104.0` and raises an actionable error otherwise.
- `redteam validate` checks the Go selection offline only (ordinary config
  parsing plus a Go-only shape check; a missing key *value* is a labelled
  pass, not an error) with zero provider calls. It cannot check key validity, endpoint
  reachability, model existence/permission, quota/429 behavior, or live model
  quality -- there is no live paid testing.
- Resume: a run records a secret-free connection fingerprint; resuming with a
  changed Go connection is refused before any new request or replay.

### Reasoning effort

Unset or empty effort keeps the provider default and preserves the historical
configuration serialization and connection identity. Explicit `none` is a
request to disable reasoning where the selected model supports it. The common
vocabulary is `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; accepted
levels and their native interpretation depend on the selected provider/model.
Unknown settings are rejected; unsupported API parameters remain explicit errors.
Selecting a level does not imply every model accepts it.

The configured value applies to conversation, planner, atomizer, reviews and
worker calls through the same assembled LLM client. It changes the existing
secret-free connection identity when explicit, so a stored Run cannot silently
resume with a different effort. Start a new Run for a changed effort.

CLI setup asks for effort after the model, or reads the existing environment.
The web settings dialog provides provider and effort selection, saves non-secret
choices, and permits returning effort to the provider default. Environment values
take precedence over saved web choices on restart. API keys remain write-only.

Wire semantics follow the existing SDK and protocol contracts, verified against
the installed LiteLLM implementation and its
[reasoning documentation](https://docs.litellm.ai/docs/reasoning_content) and
[Anthropic effort documentation](https://docs.litellm.ai/docs/providers/anthropic_effort).
Configuration and mocked wire tests are separate from a live provider acceptance
test; no paid provider calls are implied by offline validation.

Chat requests carry `reasoning_effort`; Responses requests use the SDK's native
`reasoning.effort` mapping. OpenCode Messages requests merge `output_config.effort`
with the structured format instead of replacing it. The adapter derives thinking
mode/budget from the selected SDK provider configuration and requests omitted
display when that native thinking contract applies. For custom models missing
from the SDK catalog, mapped protocol fields are forwarded explicitly; the API
still determines which capabilities the actual model accepts. An enabled SDK
`drop_params` is rejected for explicit effort because it can silently remove the
setting, including when the global flag overrides a per-call false value.

Native tool rounds retain only opaque encrypted provider continuation in the
existing assistant history/checkpoint. The LLM adapter owns capture and replay;
the orchestration loop echoes the shared `NativeCompletion.continuation` field
without interpreting provider names. The core contract requires JSON blocks,
at most 16 blocks and 1 MiB of encoded payload. Malformed continuation fails
explicitly instead of being silently dropped. This adds no separate cache or
state store; the empty default preserves older completion objects.

For Anthropic, [omitted thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)
returns an empty thinking field with an encrypted signature that must be replayed
unchanged. Plaintext thinking is rejected rather than scrubbed into a forged
signed block. Provider-specific availability remains an API concern.
