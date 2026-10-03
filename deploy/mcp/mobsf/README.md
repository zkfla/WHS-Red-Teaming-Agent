# MobSF static-only MCP

This is a repository-owned REST client exposed through four stdio MCP tools,
all bound to the existing `analysis.static_analysis` operation and Asset target.
All four tools conservatively retain HIGH risk. Submit/start require Control
approval under the default approval policy. No other REST route is exposed.

1. Provision a private API-key file **outside Git** and set `MOBSF_API_KEY_FILE`
   to its path. Never put the key value into environment variables or policy JSON.
2. Start `docker compose -p whs-mobsf-static -f deploy/compose/mobsf-static.yml up -d`.
   MobSF runs as UID/GID 9901 on an internal network. An allowlisted REST relay
   publishes `127.0.0.1:18000` and forwards only to that MobSF service. The service
   mounts only the key file; the adapter streams the one approved APK. No Docker
   socket, host home, or complete workspace is mounted into MobSF.
   Docker log retention is disabled for MobSF because its upstream startup banner
   prints the API key. The adapter emits fixed error codes and masked Evidence.
3. Create a private policy using the shape below. The `analysis` block reuses
   Track A's `AnalysisServerPolicy`. Its path is operator-owned; the model receives
   only `apk_ref` and SHA-256, never this path. Pin the policy's own SHA-256 in
   `--sha256`. Use one private state file per engagement/run. SQLite scan records
   survive MCP restarts; expired records are inaccessible. Retention cleanup is
   operator-owned; a full record quota requires operator cleanup/reprovisioning.

```json
{
  "enabled": false,
  "endpoint": "http://127.0.0.1:18000",
  "api_key_file": "/operator-private/mobsf-api-key",
  "image": "opensecurity/mobile-security-framework-mobsf:v4.4.6@sha256:72311e3553ca2c21043923cace27ed99f800cd641e9368160406779516dd774e",
  "workspace_root": "/approved-apks",
  "analysis": {"apks": {"bank-app": {"path": "/approved-apks/approved.apk"}}},
  "hashes": {"bank-app": "<approved-apk-sha256>"},
  "engagement_id": "<engagement-id>",
  "run_id": "<run-id>",
  "state_path": "/operator-private/mobsf-scans.sqlite3"
}
```

4. Configure the `REDTEAM_MCP_MOBSF_*` variables in `.env.example` and include
   `mobsf` in the configured server list. The stdio command must use the same
   pinned policy that the adapter reads. Set the policy `enabled` to `true` only
   for the approved deployment. The client connects only to the explicit numeric
   loopback origin, ignores proxy environment variables, and rejects redirects.
5. Agent exposure separately requires:

```json
{"mobsf_agent": {"enabled": true, "assets": {"bank-app": "<approved-apk-sha256>"}}}
```

The default is off. HEALTHY is required in the live Registry. The app composition
root injects a run-local ToolCatalog/Gateway view: hidden tools cannot compile,
and eligibility is rechecked before invocation. Scope and approval remain with
Control. `analysis-local` is offered only when every discovered apktool tool is
UNHEALTHY; `mobile-local` follows the same rule for mobile-next. An absent or
UNKNOWN primary does not establish that condition. Raw discovery/health stays
available for diagnostics. Fallbacks provide bounded ZIP/ADB diagnostics, not
the same result contract as apktool/mobile-next; `mobile.device_shell` needs an
explicit ROE allowlist entry before Agent execution.

`mobsf_start_scan` makes one static scan request, then polls the owned hash's JSON
report within configured limits. A timeout/cancellation records uncertainty and
does not cancel the upstream analysis or authorize automatic resubmission. Status
reads can recover a `running` record from the report. No global task/scan listing
is used. Missing report sections are explicit; invalid shapes fail closed.

Only structural manifest/network rule IDs and permission names are returned.
Secret candidates retain counts, masked values and report locations; raw values,
source text, HTML descriptions, endpoint queries and logs are omitted. A report
reference plus its source location is operator-restricted evidence, not a public
report URL. Only the explicit HIGH `app_is_debuggable` rule is currently promoted
through Expectation → Verdict → Finding. Permission, secret and network
observations alone do not confirm a vulnerability.

For actual discovery/health/invoke validation, use the existing finance-lab runner:

```powershell
.\.venv\Scripts\python.exe examples/finance_mcp_smoke.py --mobile-mcp-config <private-config.json>
```

The private config supplies existing server environments, approved artifact/device
references, output paths, and explicit prerequisite skip reasons. Docker static
servers cannot be skipped. The original finance smoke mode remains the default.
For the Agent path, set `REDTEAM_TEST_MOBSF_CONFIG` to that private config and run
`tests/integration/test_mobsf_agent.py::test_real_mobsf_docker_agent_e2e`. Its model
choices are scripted from observed scan references; Control, MCP, REST, Docker
and Evidence are real. It does not prove autonomous selection by a real LLM.

REST behavior was checked against the official [API implementation](https://github.com/MobSF/Mobile-Security-Framework-MobSF/blob/v4.4.6/mobsf/MobSF/views/api/api_static_analysis.py)
and the pinned image. Deployment follows the official [Docker options](https://github.com/MobSF/docs/blob/master/docker_options.md).
