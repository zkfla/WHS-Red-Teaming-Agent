# Shell migration preset (TASK-008): execution MCP to builtin shell provider

Ownership: Capability team. Deployment/operator config only -- no code branch
selects the migration. Applying the preset changes the *registered ownership*
(which server ids are configured plus the shell policy path); rollback restores
the previous environment. The gateway default-fallback honesty is unchanged:
no MCP servers and no shell policy still yields an honest empty catalog, and
`REDTEAM_MCP_SERVERS=example` still resolves to the explicit test fixture only
(see `src/redteam/app/bootstrap.py` honest-empty path and `EXAMPLE_MCP_SENTINEL`).

## Preset (operator environment)

```bash
# 1. Remove the replaced server id(s) from the configured set.
#    Execution triad first; recon/active/inventory/whois ids stay until the
#    TASK-005 egress proof plus TASK-008 integration proof complete.
REDTEAM_MCP_SERVERS=<retained-ids-minus-execution>  # e.g. proxy,zap,session,mobsf,...

# 2. Point at the operator-owned shell workroom policy (absolute path).
#    Unset or blank disables the shell provider.
REDTEAM_SHELL_POLICY_PATH=/absolute/path/shell-workroom-policy.json
```

Minimal policy file (JSON object matching `ShellWorkroomPolicy`; image must be
content-pinned unless `allow_unpinned_image` is explicitly true):

```json
{
  "image": "example.com/workroom@sha256:<64-hex-digest>",
  "max_jobs": 32,
  "max_output_bytes": 65536,
  "max_script_bytes": 65536,
  "default_timeout_seconds": 60.0,
  "max_timeout_seconds": 300.0
}
```

## What changes on apply

- `redteam run`, `redteam resume`, and `redteam tools` resolve
  `REDTEAM_SHELL_POLICY_PATH` through one capability-owned assembly
  (`capability/shell_runtime.py:build_shell_runtime`) and pass its adapter,
  concurrency, output limits, and program source into the existing generic
  `builtin_adapters` / `shell_programs` seams. No new registry, no
  server/tool-name conditions.
- Shell tools (`analysis.shell_run`, `analysis.shell_job_start/status/cancel`)
  appear with default risk MEDIUM/MEDIUM/LOW/MEDIUM. Approval fires only
  through explicit operator rules (`require_approval_for`, `risk_threshold`,
  `command_approval_rules`) or matching command-pattern rules; ambiguity
  alone stays ALLOW with budget/rate metering charged.
- Program cards for image/instance executables stay on the existing CARD-kind
  `program` API (`program.list` / `program.describe`); `redteam tools list`
  shows callable gateway tools only and never triggers a live workroom query.
- Retained MCP servers (proxy, zap, session, mobsf, mobile, nuclei, web-http,
  vdb, workspace, search/page-text, finance, network, inspection) keep their
  `MCPAdapter` + manifest + profile + `REDTEAM_MCP_*` path with no change.

## Rollback (config only, no redeploy)

```bash
REDTEAM_MCP_SERVERS=<previous-value-including-execution>
# unset REDTEAM_SHELL_POLICY_PATH
```

The execution MCP server code (`capability/servers/execution.py`,
`deploy/mcp/execution/`) stays in-tree until the replacement proofs complete,
so rollback restores the configured provider set and policy path. The gateway
rejects duplicate server ids; the explicit removal in this preset controls
which provider owns the migrated functionality.

## Dual-operation warning: removal must be explicit

The execution MCP `server_id` and `shell-workroom` are DIFFERENT ids, so the
gateway happily registers BOTH at once -- the duplicate-`server_id` guard
(`capability/bootstrap.py` builtin loop) only rejects two providers claiming
the SAME id and does NOT prevent dual ownership. If the operator sets
`REDTEAM_SHELL_POLICY_PATH` while leaving the replaced id (e.g. `execution`)
in `REDTEAM_MCP_SERVERS`, both the MCP triad and the shell tools serve side
by side. The preset therefore requires EXPLICITLY REMOVING each replaced id
from the configured set on apply; rollback re-adds the ids (plus unsetting
the shell path) with the same explicitness. No code branch selects the
migration -- the configured id set plus the shell policy path IS the
migration state.

## Verification checklist

- `redteam tools list` with the preset shows `analysis.shell_*` at
  MEDIUM/MEDIUM/LOW/MEDIUM and no `example` tool; without the preset and
  without MCP servers it shows the honest empty catalog with a warning.
- Clean default shell script evaluates ALLOW with one budget action charged;
  explicit namespace `require_approval_for`, explicit low threshold, and
  explicit command rules each hold; broken risk resolution escalates to
  human (`risk:unresolved`), never silent ALLOW.
- Existing gates stay green: `pytest -q`, `ruff check`, `mypy`, both compose
  `config -q`, `mcp-example` build, finance-lab `smoke` + `agent-smoke`.
