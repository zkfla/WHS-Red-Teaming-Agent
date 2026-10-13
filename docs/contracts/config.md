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
| `REDTEAM_ORCHESTRATION_MAX_RETRIES` | `3` | integer >= 0 |
| `REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION` | `false` | boolean (`true`/`false`) |

Interactive, `run`, and `resume` share this loader. `--max-steps` takes
precedence over the environment value. Library callers can still supply an
explicit `OrchestrationConfig` without reading the environment.

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

See [workroom deployment](../../deploy/docker/shell-workroom/README.md)
for first-use requirements and isolation constraints.

## LLM providers: `litellm` and `opencode-go`

`LLMConfig` has exactly three fields (`provider`, `default_model`,
`api_key_env_var`) read from `REDTEAM_LLM_PROVIDER` / `REDTEAM_LLM_MODEL` /
`REDTEAM_LLM_API_KEY_ENV_VAR` -- selecting the Go adapter needs no new
variable and no multi-profile system (zero schema change).

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
