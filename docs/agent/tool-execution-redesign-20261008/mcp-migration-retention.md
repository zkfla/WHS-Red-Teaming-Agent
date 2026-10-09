# MCP migration / retention decision (TASK-004)

Per-feature verdict on which MCP capabilities the Kali/shell provider
replaces versus which stay because they need genuine external
connectivity. Code evidence carries file:line anchors. Executing the
migration and registration changes is TASK-008, not this task; the
ownership and registration change per row tells that task what to do.

## Verification boundary (applies to every row)

- At the network-none stage, program execution verification is
  offline/loopback LOCAL-ONLY. Claiming a shell program replaces an
  MCP capability for target traffic requires TASK-005 egress plus
  TASK-008 integration proof; until then replacement rows are
  candidates, not completions.
- Installing a program, or showing `--version`/help output, is never
  presented as a full replacement of an MCP capability.
- Privilege boundary: the workroom runs non-root with --cap-drop ALL
  (no CAP_NET_RAW). `tcpdump` live capture and `nmap -sS` raw SYN are
  NOT usable; offline `tcpdump -r <file>` and `nmap -sT` connect scan
  are. Each replacement row records its real usable mode.

## Single sources (reconciled with TASK-003)

- Image build and program cards consume one source: packages.txt
  (deploy/docker/shell-workroom/packages.txt) crossed with
  dpkg-query/apt-cache/dpkg -L on the built image, generated as
  deploy/docker/shell-workroom/program-manifest.json with per-package
  executables, versions, origins, and usable_mode rows.
- Baseline install set: nmap, curl, jq, git, python3-pip, dnsutils,
  openssl, netcat-traditional, whois, tcpdump, sqlmap, wordlists
  (rockyou), plus mechanism deps procps and util-linux, on base
  bash/python3/coreutils. Deliberately excluded: iputils-ping,
  iproute2, nikto, metasploit.
- Baseline usable modes: `nmap -sT` only; `tcpdump -r` only; `dig`
  (dnsutils), `curl`, `openssl s_client`, `whois`, `sqlmap`, `jq`,
  `git`, `nc.traditional` are CLI-usable; venv bins need explicit
  activation and are not on PATH.
- Runtime PATH inside the workroom: <workdir>/.local/bin:
  /usr/local/bin:/usr/bin:/bin (instance bins resolve by bare name;
  cards additionally carry absolute executable paths).

## Replacement candidates (shell/Kali provider territory)

Ownership for every row in this section: Capability team (shell side).
Registration change (TASK-008): drop the MCP server from the configured
server set and register the builtin shell adapter through the existing
builtin_adapters seam; risk/operation bindings move to the shell manifest
whose operations are deliberately distinct from the execution MCP ones
(src/redteam/capability/shell_manifest.py:1-14).

| MCP capability | Evidence | Shell equivalent (usable mode) | Limits |
|---|---|---|---|
| execution one-shot commands + job triad | src/redteam/capability/servers/execution.py:647 run_command, :746 job_start, :789 job_status, :807 job_cancel | shell_run plus shell_job_start/status/cancel (arbitrary Bash/Python scripts, sync plus job IDs) | Scripts are not allowlisted one-shot commands; starting shell work is MEDIUM, polling LOW, cancel MEDIUM. The existing operator approval threshold and explicit command rules decide whether an invocation requires approval. Local execution is network-closed; target execution uses the separate configured gated descriptors. |
| recon DNS probes | src/redteam/capability/servers/recon.py (DNS/TLS/HTTP probes, target traffic) | `dig` (dnsutils) | Generates target traffic: needs TASK-005 egress before the replacement is real. |
| recon TLS inspection | src/redteam/capability/servers/recon.py | `openssl s_client` | Same egress gate as above. |
| recon HTTP probes | src/redteam/capability/servers/recon.py | `curl` probes plus `jq` shaping | Same egress gate as above. |
| recon-active Nmap wrapper | src/redteam/capability/servers/active.py (narrow Nmap, fixed ports, no scripts) | `nmap -sT` connect scan only | `-sS` raw SYN is NOT usable (no CAP_NET_RAW). Same egress gate as above. |
| endpoint inventories (openapi/oidc/html) | server_roles.py recon-passive operations | `curl` fetch plus script shaping | Same egress gate as above. |
| whois lookups | baseline whois package | `whois` CLI | Target-side queries count as target traffic: same egress gate. |

## Retained (genuine external API/DB/device connectivity)

Retention is decided on connectivity grounds, never on tool names. Kept
servers stay on the existing MCPAdapter plus manifest plus profile plus
REDTEAM_MCP_SERVERS path with no registration change.

| MCP capability | Evidence | Why shell cannot replace it |
|---|---|---|
| web-proxy captures | src/redteam/capability/servers/proxy.py (proxy-product-neutral HAR facade) | Reads operator-exported proxy captures; needs the capture pipeline, not a shell. |
| web-zap live captures | src/redteam/capability/servers/zap.py, BUILTIN_LAUNCHER_ROLES web-zap in src/redteam/capability/server_roles.py | Needs a live ZAP service and its capture pages. |
| burp / chrome-use / cua externals | deploy/mcp/external/burp.env.example, chrome-use.env.example, cua.env.example, zap.env.example | Real external scanners and browsers with their own credentials and stateful sessions (engagement-scoped gateway). |
| session cookies | src/redteam/capability/servers/session_state.py | Reads operator-exported Playwright cookies without exposing them; a shell has no equivalent secret boundary. |
| mobsf analysis | src/redteam/capability/servers/mobsf.py:28 TOOLS (four REST routes), src/redteam/capability/adapters/mcp/mobsf.py (real REST probe) | External MobSF service API. |
| mobile external candidates | src/redteam/capability/servers/mobile.py, analysis.py (candidate apktool/mobile-mcp servers) | Device and app-launch connectivity (app launch, device I/O). |
| mobile/analysis local fallbacks | src/redteam/capability/servers/mobile.py, analysis.py | Already in-process; the image carries no equivalent toolchain (no apktool), so no shell replacement exists yet. Revisit only with a toolchain decision. |
| nuclei passive matching | src/redteam/capability/servers/nuclei.py | Needs the nuclei binary, absent from the image; excluded-tool territory (niko/metasploit class decision applies). |
| web-http session accounts | src/redteam/capability/server_roles.py web-http (forwards_session_credentials=True) | Session-credential forwarding has no shell-provider equivalent contract; retain until one exists. |
| vdb knowledge | src/redteam/capability/servers/vdb_readonly.py (pinned search library), vdb_extended.py, deploy/mcp/vdb | Data dependency on the AppThreat VDB library, not a runnable program. |
| workspace IO | src/redteam/capability/servers/workspace.py, deploy/mcp/workspace | Operator-scoped bounded IO with atomic descriptor-relative writes; policy surface, not shell territory. |
| web search / page text | src/redteam/capability/servers/tavily_search.py, web_search_mcp.py, page_text.py, page_text_mcp.py | External search provider API and public web fetch (external connectivity by definition). |
| finance lab | deploy/mcp/finance | Lab target harness with its own auth and apps. |
| network bounded primitives | src/redteam/capability/servers/network.py | Pinned-address primitives with execution policy; shell replacement inherits the egress gate and is tracked with the recon rows above, so the MCP shape stays until then. |
| inspection helper | src/redteam/capability/servers/inspection.py | Bounded single-response observation with no secondary requests; local helper shape, no shell equivalent needed. |

## Replacement verification checklist (for TASK-005/008)

- Offline/loopback proof now: each baseline executable runs (`--version`
  plus a loopback-only functional check) in the built image.
- Privilege proof now: `nmap -sS` and live `tcpdump` fail as expected
  (no CAP_NET_RAW); `nmap -sT` and `tcpdump -r` succeed.
- Target-traffic proof later: dig/curl/openssl/nmap/whois against an
  isolated target through the TASK-005 egress with metering, matched
  against the MCP equivalent behavior being replaced.
- Registration proof later: builtin adapter serving shell_run/job triad
  through Control admission, Work lease, and evidence, with the execution
  MCP server removed and no test pinned to its tool names.

## TASK-004A analysis (sequel, not implemented here)

Recorded facts for the sequel owner, verified against the current tree:

- verify.py evidence metadata already stores the original full ToolResult
  plus the proposal plus instance/work identity. A targetless call yields
  not_applicable (orchestration/observations/mapper.py:257-261); no_mapper
  is an unknown tool_id with a target present (mapper.py:262-269). Both
  mean no graph write.
- graph_recording.py _write_fact requires evidence.metadata.target_node_id
  and only observes the same target; shell tools carry requires_target
  False and therefore no such metadata, so a shell stdout/JSON claim read
  through the existing graph.record raw path is rejected as evidence
  naming no target.
- Consequence: at the network-none stage a local shell can preserve raw
  evidence and hypotheses but cannot confirm remote-target observation.
  The 004A sequel needs a real target-subject contract supplied by the
  responsible execution/egress producer (TASK-005). It must not be solved
  by assigning a fake target or by auto-treating a stdout URL as
  observed.
- Proposed shape (for 004A/005 to adopt or refuse): keep evidence-only
  preservation as the default now (already works through the evidence
  store); add a generic envelope-shape mapper contribution once the
  target-subject contract exists, keyed by envelope shape and never by
  tool name, with no_mapper continuing to mean not-mapped rather than
  success (FR-009).

## What TASK-004 delivered (this task)

- Four-kind card/detail discovery over one source per kind, shared filter
  rule, paginated list contract, trusted-subject program reads, and the
  RuntimeDeps.shell_programs seam with the structural ShellProgramDiscovery
  port both sides share without importing each other.
- Lazy model exposure (cards in context, schemas after detail) for catalog
  tools and local helpers alike, with provider-declared bootstrap flags
  and an unwired-lookup fallback that preserves legacy runs.
- This decision file. Executing migrations and app wiring belongs to
  TASK-008; observation mapping belongs to TASK-004A/005.
