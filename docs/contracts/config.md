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

### Model decision recovery budgets

The decision chain separates normal calls, output correction and transport
retries. The configured normal allowance is 12 and output-correction allowance
is 6; their sum remains the total 18-request ceiling for that chain. Transport
retries have a separate cap of 5, consumed inside the same total rather than
added to it. The Run request/token ledger and absolute 900-second chain clock
also apply; each SDK call is bounded at 180 seconds, clipped by remaining time.
SDK retries remain zero. A transport retry preserves the current request and
any prior output-correction feedback, without appending a provider error as
model-output correction feedback. Target tools are not automatically replayed.

| Environment variable | Default | Validation |
|---|---|---|
| `REDTEAM_DECISION_NORMAL_LIMIT` | 12 | 1–32 |
| `REDTEAM_DECISION_MAX_ATTEMPTS` | 6 | 1–6, output-correction allowance |
| `REDTEAM_DECISION_TRANSPORT_MAX_ATTEMPTS` | 5 | 0–5, zero disables transport retries |
| `REDTEAM_DECISION_TIMEOUT_SECONDS` | 900 | finite, greater than 0, at most 3600 |
| `REDTEAM_LLM_ATTEMPT_TIMEOUT_SECONDS` | 180 | finite, greater than 0, at most 900 |

`DecisionBudget` supplied explicitly without a transport cap retains its
legacy counter interpretation. New default `StructuredDecisionClient` calls
use a separate transport cap. Its standalone six-attempt total stays six,
including transport retries; it is not multiplied by the retry cap.
`ConversationAssistant` has its own constructor limits: 180 seconds total,
six requests total and five transport retries within that total. It does not
read global orchestration settings itself. Cancellation and admission denials
are not recovery requests.

`ConversationError.diagnostic` preserves the safe `DecisionDiagnostic`,
including the nested cause, public validation rules/locations, provider status,
stage and attempt counters. `InputReceipt.diagnostic` attaches that cause to
the original input and binding. CLI failure notices use those fields instead
of exception messages. Provider bodies, prompts, keys and private reasoning
are not diagnostic fields. This preserves the reason for an error; it does not
promise recovery from a permanent provider failure.

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

Kali communication is independently closed, inherited, imported from a legacy
network selection, or explicitly prepared for the selected origins.
Closed overrides inherited environment settings, including manual MCP selections
(`metadata.kali_network_mode: closed`). The form's prepare mode records
`metadata.web_policy.kali_network_mode: prepare` and a validated
`kali_network_preparation` object. CLI startup consumes the same engagement metadata;
neither path mutates `.env` or the global Docker context.

`TargetNetworkPreparation` contains the Docker context and optional full bridge ID.
Review shows these route inputs together with the selected origins and ports. At Run
startup, preparation verifies the Docker endpoint, Linux engine, selected bridge,
and running target-container attachment. It does not build a sidecar, inspect `nft`
kernel features, contact the target, or prove reachability.

The target shell tool checks its declared target and port against the prepared
selection, approved target and common scope, then attaches the Kali workroom to the
selected Docker bridge for that synchronous execution. It detaches the bridge when
the execution ends. There is no packet-level IP/port allowlist, byte/flow quota,
NFQUEUE daemon or gate sidecar; an arbitrary script can technically reach other
addresses visible from that bridge. Local shell tools stay network closed.

Conversation inputs receive configured, unverified server declarations before
a Run and finalized catalog/health data while attached to a live Run. Declaration
inspection performs no MCP or Docker startup. Planner and worker contexts receive
the generated target permissions; tool discovery does not replace Control admission.

## Kali target addresses and Docker routes

Target origins and registry pins use their actual direct IPv4 addresses. No
loopback, address, port or DNS alias translation is implicit. `127.0.0.0/8`
inside a workroom names that workroom, not the operator machine or Docker host;
target preparation refuses it. Remote Docker contexts route from the remote
daemon, so an operator's local loopback is never interpreted as VM loopback.
Choose a reachable host/interface address and port explicitly. If a host service
listens only on loopback, a verified operator deployment/forwarding choice is
needed; this implementation does not create a relay or claim its identity.

For targets in another container on the same daemon, prepare mode additionally
accepts `kali_network_preparation.target_network_id`: the existing bridge's full
64-character Docker ID. Select the target's actual container IPv4 and listening
port as the origin. The matching policy fields are `target_network_id` and
`target_ports` (registry name to port tuples). HTTP transport and Kali container
transport have separate reachability; choosing a bridge does not make its private
IP reachable from a host-side HTTP provider.

Preparation verifies each pinned IP against the selected bridge's current
membership and running container's ID, image and attachment. The identity is
part of the existing preparation fingerprint and is checked again before direct
execution. Scope and logical approved target/port are checked separately;
pins still must match selected host resolution. Changed Docker endpoint,
container identity, pins, ports or selected network cannot use the original
prepared environment. Historical prepared network identities that lack the new
route binding are refused on resume; reports remain readable and no data is
migrated or overwritten. Local closed policies retain their serialized defaults.

Opening joins the selected bridge by ID. Closing detaches the workroom, but never
removes the operator's bridge or target containers. A detached workroom with no
data networks remains eligible for closed reuse; a same-named bridge with another
ID is rejected. Only the synchronous target-run tool receives this route; background
target jobs and package-install routes are not advertised.

Catalog target schemas advertise selected registry names and ports. Model scripts
retain zero capabilities: NET_ADMIN and raw-packet modes remain unavailable, and
network selection does not authorize a privileged run. Native HTTP restrictions
and common scope, ROE and approval checks remain separate from the direct Kali route.

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
The managed image profile is `baseline` unless
`REDTEAM_SHELL_IMAGE_PROFILE=kali-default` is selected explicitly. The latter
uses the repository-owned Kali standard metapackage input; it is never a
fallback and cannot be combined with an explicit shell policy. Profile
selection changes build/preparation identity, not Control permission or target
scope. No profile installs packages during an investigation run.
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
Direct target preparation resolves and fingerprints the Docker endpoint used by
the shell so readiness and execution cannot silently switch daemons. Closed local
execution retains its prior behavior.

Target-network preparation records `network_environment_fingerprint` in the same
component binding. Its private provider-cache record binds target IPs and ports,
the selected bridge and target-container identity, Docker endpoint, and prepared
shell identity. Resume rechecks that identity; changed selections cannot silently
replace the original environment for that Run.

Central Runs with a pinned LLM connection, execution or network environment also keep
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

## Program execution metadata and selection

The existing shell-program discovery provider optionally implements
`ProgramExecutionDiscovery.describe_execution_environment()` (metadata version
1). Its Capability-owned snapshot describes local/target/install routes, actual
descriptor IDs, selected pins/ports, purpose limits and unsupported privilege
modes. Default local execution is network-none; an unconfigured target/install
route is unavailable. Prepared route/context identities yield `target_ready`
(or `install_ready` when a mirror/proxy and nonzero capacity exist). Legacy
configured routes without those preparation identities stay
`configured_unverified`. This is declaration/preparation metadata: it neither
proves reachability nor grants permission. Unknown health remains unknown, and
every action still passes Control, schema/scope binding and provider checks.
Providers without the optional seam report unknown metadata; no target
readiness is inferred from inventory or registration. Eligible catalog IDs
filter route IDs for the current role.

Program inventory uses five distinct meanings. `installed` means the immutable
image probe observed a package-owned executable. `route configured` describes
provider setup only. `mode supported` accounts for declared network/capability
requirements and can remain false even for an installed program. `execution
admitted` is a per-invocation Control/provider decision. `execution observed`
requires an actual result/evidence record. None is inferred from an earlier
state. Manifest PATH status and static requirements are metadata, not health,
reachability, permission, or successful execution.

Worker context, Planner/atomizer payload, `agent.readiness` and `program.describe`
share this projection. Passive context contains at most eight program samples
and prioritizes callable/reference cards within its existing 100-card ceiling.
Counts/status/search guidance retain the full inventory. Samples are not an
allowlist: `program.list` and `tool.list_cards` still search/page the owner's
whole current manifest/instance, and `program.describe` returns the executable
and route constraints without claiming every program flag is usable.
Already-supplied execution schemas need no discovery round. No per-program
wrappers, title-based tool switch, extra registry or model approval polling is
introduced; the native input bound and shared model-request ledger remain.

`native-probes` is version 27 (worker prompt 13, strategy prompt 15, atomizer prompt 8).
The explicit single-stage compatibility profile is version 20 (planner prompt 7,
shared worker prompt 13). The code contracts additionally bind
`planner_request_contract_version=4` and `worker_conversation_version=4`, including
for custom profiles. Worker checkpoints use session version 2; see
[Worker lifecycle](worker-conversation.md) and
[Planner request and resume semantics](planner-request.md).
Discovery and role-readiness metadata contributions use version 2. Prompt
hashes, role/tool and extension fingerprints continue to reject incompatible
execution resume rather than changing historical Run selections. See
[program evidence](evidence.md#program-originals-and-protected-network-linkage)
for original-output delivery and the boundary between attempts and claims.
