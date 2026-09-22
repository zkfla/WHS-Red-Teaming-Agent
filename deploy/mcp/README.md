# MCP Server deployment notes

Owned by Capability Team.

For the runnable four-service financial lab, tool inventory and exact limitations,
see [finance/README.md](finance/README.md). It uses a separate Compose file and
synthetic bank; the default example stack below remains the minimal smoke target.

This stage ships exactly one MCP server: `example-mcp`, a harmless
server used to validate discovery and invocation over MCP Streamable
HTTP. It exposes a single `ping` tool at `http://localhost:8765/mcp`.
`ExampleMCPAdapter` remains an in-process test double; real endpoints
use one of the Web/API, Mobile, Recon, or Analysis adapters.

## Naming conventions

- Container name: `redteam-mcp-<server-id>` (e.g. `redteam-mcp-example`)
- Network name: `redteam-net`
- Volume policy: MCP server containers should be stateless where
  possible; if a server needs persistent state, mount a named volume
  under `redteam-mcp-<server-id>-data` and document it here.

## Health check

Each MCP Adapter performs a protocol-level connection health check.
The Compose healthcheck separately verifies that the container is
listening so Docker can manage its lifecycle without embedding a
second application-specific health API.

## Tool identity and registration

Remote tools are exposed as deterministic framework IDs:

```text
<capability-namespace>.<normalized-remote-tool-name>
```

Examples: `web.send_http_request`, `mobile.inspect_apk`, `recon.probe_http`,
`analysis.compare_response`. Adapter discovery
returns a complete server snapshot and `MCPToolRegistry.replace_server_tools`
atomically replaces only that server's tools. A conflicting `ToolId`
owned by another server is rejected instead of silently overwritten.

## Server and transport configuration

`CapabilityConfig.mcp_servers` contains stable server IDs. Each ID is
resolved from environment variables before discovery:

```text
REDTEAM_MCP_<SERVER_ID>_KIND=web_api|mobile|recon|analysis
REDTEAM_MCP_<SERVER_ID>_TRANSPORT=streamable_http
REDTEAM_MCP_<SERVER_ID>_ENDPOINT=https://mcp.example/mcp
REDTEAM_MCP_<SERVER_ID>_RISK_LEVEL=low|medium|high|critical
REDTEAM_MCP_<SERVER_ID>_MAX_CONCURRENCY=1
REDTEAM_MCP_<SERVER_ID>_MAX_OUTPUT_BYTES=1048576
REDTEAM_MCP_<SERVER_ID>_TOKEN_ENV_VAR=NAME_OF_SECRET_VARIABLE  # optional
```

Server IDs use lowercase kebab-case, and hyphens become underscores in
the environment prefix; `web-example` maps to
`REDTEAM_MCP_WEB_EXAMPLE_*`. Plain HTTP is accepted
for local hosts only. An authorized non-local lab must explicitly set
`REDTEAM_MCP_<SERVER_ID>_ALLOW_INSECURE_HTTP=true`.

The token setting contains the *name* of a secret environment variable,
not a token. The Adapter sends that value as a Bearer token without
placing it in AppConfig, tool metadata, or results.

`MAX_OUTPUT_BYTES` limits the UTF-8/compact-JSON size allowed to cross the
Gateway boundary. Oversized MCP output is omitted and returned as
`tool_output_too_large`; it is never partially truncated into a misleading
result. The default is 1 MiB and the allowed configuration range is 1 KiB to
16 MiB.

For a local MCP process, use argv-based stdio configuration. No shell is
invoked, arguments must be a JSON string array, and only explicitly named
environment variables are passed through:

```text
REDTEAM_MCP_<SERVER_ID>_KIND=mobile
REDTEAM_MCP_<SERVER_ID>_TRANSPORT=stdio
REDTEAM_MCP_<SERVER_ID>_COMMAND=/absolute/path/to/python
REDTEAM_MCP_<SERVER_ID>_ARGS_JSON=["/absolute/path/to/server.py"]
REDTEAM_MCP_<SERVER_ID>_PASSTHROUGH_ENV=ANDROID_SERIAL,ANDROID_HOME
```

`TRANSPORT` defaults to `streamable_http` for existing configurations.

## Mobile capability (candidate: mobile-next/mobile-mcp)

**Not finalized.** `mobile-next/mobile-mcp` (https://github.com/mobile-next/mobile-mcp)
is being evaluated, not adopted — `MobileMCPAdapter` only knows the generic
`MCPServerAdapter`/`MCPTransport` shapes, so swapping it for a different
mobile MCP server later means changing `.env.example`/`examples/app.yaml`
only, no Adapter code.

Canonical operations this capability binds today (see
`docs/contracts/actions.md`): `app_launch`, `device_shell`,
`response_analysis`. `runtime_instrumentation` is a canonical operation
this capability does **not** yet implement — see "Known gaps" below.

| canonical operation | mobile-next/mobile-mcp remote tool | notes |
|---|---|---|
| `app_launch` | `mobile_launch_app` | starts an app by package/bundle name |
| `response_analysis` | `mobile_get_device_logs`, `mobile_list_crashes`, `mobile_get_crash` | observing device logs/crash output after an action; observation only |
| `device_shell` | *(not provided upstream)* | filled by the in-repo fallback below |

Bind only these remote tool names via `REDTEAM_MCP_<SERVER_ID>_ALLOWED_TOOLS_JSON`
+ `_TOOL_OPERATIONS_JSON` — see `.env.example` for the exact values. UI
automation tools the upstream server also exposes (`mobile_click_on_screen_at_coordinates`,
`mobile_swipe_on_screen`, `mobile_take_screenshot`, `mobile_install_app`,
`mobile_terminate_app`, ...) are real remote tools `discover_tools()` will
surface, but they are **not** bound to any canonical operation in this PR —
tap/swipe/screenshot-style interaction is not yet a canonical operation.
Propose one via Issue (docs/contracts/actions.md's process) before wiring
any of them up.

mobile-next/mobile-mcp supports both transports: stdio (its default,
`npx -y @mobilenext/mobile-mcp@1.0.4`, run directly on a host with
ADB/Xcode tooling already set up) and Streamable HTTP (`--listen <port>`,
containerized via `deploy/docker/mobile-next-mcp` + the `mobile-next-mcp`
compose service, profile `mobile`, `network_mode: host` since it needs to
reach adb/simulators on the host). Either way it needs a real,
operator-authorized device or emulator — there is no lab/dummy mode.

### Local fallback: `redteam.capability.servers.mobile` (server ID example: `mobile-local`)

Implements only `device_shell`, the one canonical operation neither
mobile-next/mobile-mcp nor any other candidate currently covers: one fixed,
allowlisted ADB diagnostic (`getprop`, `pm_list_packages`,
`dumpsys_battery`, `logcat_recent`) against an operator-registered device
serial — not a general remote-shell primitive. Policy shape:

```json
{"devices": {"pixel": {"serial": "emulator-5554"}}, "allowed_commands": ["getprop"]}
```

Run it the same way as any stdio MCP server:

```sh
python -m redteam.capability.servers.mobile --policy /absolute/path/mobile-policy.json
```

### Known gaps

- `runtime_instrumentation` (e.g. Frida-style hooking of a running process)
  is not implemented anywhere in this capability. It is a fundamentally
  different class of tool than a bounded diagnostic command — building it
  in-repo would mean writing new attack tooling rather than wrapping an
  existing one (instruction.md section 21). Needs a dedicated MCP
  adapter/Issue once the team picks a real instrumentation MCP server.

## Analysis capability (candidate: apktool-mcp-server)

**Not finalized.** `apktool-mcp-server` (https://github.com/zinja-coder/apktool-mcp-server)
is being evaluated, not adopted — same swap guarantee as above via
`AnalysisMCPAdapter`.

Canonical operation this capability binds today: `static_analysis`.

| canonical operation | apktool-mcp-server remote tool | notes |
|---|---|---|
| `static_analysis` | `decode_apk`, `get_manifest`, `get_apktool_yml`, `list_smali_directories`, `list_smali_files`, `get_smali_file`, `list_resources`, `get_resource_file`, `search_in_files` | decompiled-code-level read/analyze tools only |

`build_apk`, `modify_smali_file`, `modify_resource_file`, and
`clean_project` are **deliberately excluded** from
`REDTEAM_MCP_<SERVER_ID>_ALLOWED_TOOLS_JSON` (see `.env.example`) — this PR
only wires up inspection, per instruction.md section 21 (no build/modify
capability without a separate, explicit decision).

apktool-mcp-server only speaks MCP stdio upstream (`uv run
apktool_mcp_server.py`; no HTTP mode documented). Containerized via
`deploy/docker/apktool-mcp` + the `apktool-mcp` compose service (profile
`tools`); since it's stdio, that image is built once and invoked per-call
(`docker compose run --rm -i apktool-mcp`), not started with `docker
compose up`.

### Mobile/analysis discovery conformance

`redteam.capability.mobile_analysis_manifest` records the selected candidate
tools' ToolId, canonical operation, required/optional arguments, asset target
kind, risk level and advertised output schema where one exists. The candidate
versions used to check those argument lists are `@mobilenext/mobile-mcp@1.0.4`
and apktool-mcp-server commit `12bb33134af905923a0a58643dcc35fd694ad6f6`.
Update the manifest and pin together whenever a candidate changes.

The in-repo `mobile-local` and `analysis-local` MCP servers advertise specific
Pydantic output schemas. The external mobile-next and apktool candidates do
not advertise output schemas for the selected tools; the manifest leaves those
unknown instead of claiming a shape. The Capability transport preserves an
advertised output schema, and `MCPToolGateway.list_candidate_metadata()`
projects every registered MCP tool and canonical operation into candidate
metadata: ToolId, capability, operation, required/optional arguments, target
kind when known, risk level, and output schema when available. Missing schema
fields remain `None`; they are not inferred. For explicitly configured
mobile-next, apktool, mobile-local, and analysis-local candidates, bootstrap
checks the manifest against discovery before registration and fails on drift.
The Core `ToolDescriptor` and `ToolGateway` Port still do not expose this
candidate view, so Agent use needs a PM-reviewed Contract change.

`tests/unit/test_mobile_analysis_manifest.py` checks the local servers' real
MCP discovery on every run. To check an installed external candidate, set
`REDTEAM_TEST_MOBILE_NEXT_COMMAND` and `REDTEAM_TEST_MOBILE_NEXT_ARGS_JSON`,
or `REDTEAM_TEST_APKTOOL_COMMAND` and `REDTEAM_TEST_APKTOOL_ARGS_JSON`, then
run that test file. The external check only lists tools; it does not contact a
device or analyze an APK.

For an apktool result smoke test, call `decode_apk` on a permitted test APK,
then `get_manifest` with the returned project directory. Leave resource
decoding enabled (`no_res=false`); with `no_res=true`, the manifest remains
binary and `get_manifest` cannot parse it as text. On Windows, some
`apktool.bat` distributions pause for input at exit. Use a noninteractive
apktool launcher on `PATH` or the Docker image before interpreting an MCP
timeout as a server failure. A local direct MCP smoke on InsecureBankv2.apk
confirmed both calls on apktool 2.9.3. It did not exercise Agent selection or
the Core `ToolGateway` Port.

### Local fallback: `redteam.capability.servers.analysis` (server ID example: `analysis-local`)

A dependency-free complement to apktool-mcp-server: reads an
operator-registered `.apk` file's zip container directly (stdlib
`zipfile`) — entry listing, a SHA-256 digest per entry, and a bounded
fixed-string search — without needing apktool/Java installed. It does not
decompile smali or parse binary `AndroidManifest.xml`; that stays apktool's
job. Useful standalone, or while apktool-mcp-server isn't wired up yet.
Policy shape:

```json
{"apks": {"sample": {"path": "/absolute/path/app.apk"}}}
```

```sh
python -m redteam.capability.servers.analysis --policy /absolute/path/analysis-policy.json
```

## Adding a new MCP server

1. Add the server's image/build to `deploy/compose/docker-compose.yml`
   (see the `mcp-example` service for the shape).
2. Select `web_api`, `mobile`, `recon`, or `analysis`; the bootstrap selects
   the matching adapter. Implement a custom `MCPAdapter` only when the server
   needs non-standard normalization.
3. Add the stable server ID to `capability.mcp_servers` and configure
   its `REDTEAM_MCP_<SERVER_ID>_*` environment variables. Application
   bootstrap discovers and registers its `ToolDescriptor`s.
4. No other code should need to change — that's the point of the
   Adapter pattern (instruction.md section 11).

Real attack-tool MCP servers are explicitly out of scope for this
stage (instruction.md section 21).
