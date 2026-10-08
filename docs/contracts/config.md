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

LLM connection retry waiting is configured through the existing `LLMConfig`
snapshot, not re-read by individual agents. `provider_retry` is an additive
typed field (old configurations load its defaults). CLI operators may set
`REDTEAM_LLM_RETRY_POLICY_JSON`, for example:

```sh
export REDTEAM_LLM_RETRY_POLICY_JSON='{"base_delay_seconds":1,"max_delay_seconds":30,"max_wait_seconds":30}'
```

These are the defaults. `max_delay_seconds` caps fallback exponential backoff;
a valid server `Retry-After` is never shortened. `max_wait_seconds` bounds total
cooldown waiting per structured decision, and can be zero to defer immediately.
Actual request caps and cancellation/deadline/token guards remain independent.
Unknown fields, negative delays and non-finite values are rejected without
echoing the input. Retry policy is passed by App bootstrap to the LLM adapter;
it is not an SDK completion argument. A client owns its in-process shared
cooldown; this is not an account-wide/distributed rate limiter or persisted
cooldown. Explicit quota errors require operator action, not retries.

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
