# External Web MCP profiles

These profiles connect operator-owned desktop tools to the Capability Registry.
They do not change scope, approve an action, or make a scanner alert a verified
finding.  The default synthetic finance lab remains independent.

## Common flow

1. Install and start one upstream product on the operator host.
2. Copy only that product's `.env.example` values into the application
   environment and replace absolute paths/secrets.
3. Add its server ID to `capability.mcp_servers` for the authorized engagement.
4. Run the application validation command, then inspect discovered candidate
   metadata before allowing the Agent to execute anything.
5. Test against a synthetic target first.  Record the upstream version and
   re-run discovery whenever the product is upgraded.

`examples/external-web-app.yaml` lists all profiles for documentation.  It is
not a ready-to-run financial engagement and intentionally has an empty host
scope.

## ZAP official MCP

Install ZAP's **MCP Integration** add-on, enable the server, require a security
key and bind it to loopback.  The example uses local HTTP because the current
framework transport does not accept a custom ZAP-generated CA bundle.  Never
publish port 8282 or use this HTTP profile across a network.

The same upstream is registered twice:

- `zap-recon`: context/spider lifecycle and endpoint-discovery observations;
- `zap-web`: active/passive scan lifecycle and report generation.

This split preserves separate namespaces and risk tiers.  ZAP alerts and sites
are MCP **resources**, while the current framework gateway exposes MCP tools
only.  Until resource support is added, keep the existing scoped `web-zap`
capture facade for observation data and treat official ZAP MCP as scan control.
ZAP's current MCP add-on does not authenticate to target applications itself.

## Burp official MCP

Install the PortSwigger MCP extension inside Burp and use its packaged stdio
proxy JAR.  `burp-observe` exposes only proxy history.  Request-sending,
Repeater, Intruder, configuration editing, intercept control and editor
mutation remain excluded because they accept target or raw-message data
outside this repository's `target_ref` policy.

`burp-oast` is optional and Burp Professional-only.  It exposes Collaborator
payload creation and polling as a separate high-risk profile.  The payload must
still be inserted through a separately approved, scoped HTTP action.

## chrome-use

Install the CLI and Chrome extension, run `chrome-use extension install`, and
verify `chrome-use doctor` plus an interactive snapshot before registration.
The profile excludes JavaScript evaluation, network rewriting, upload and
download.  Click/type/press are bound only to `web.auth_attempt` and therefore
must pass Control approval.

Use a dedicated Chrome profile containing test accounts only.  A human enters
passwords, certificates and MFA values; they must not appear in Agent arguments,
MCP output or evidence logs.

## Cua Driver

Install Cua Driver, grant the OS Accessibility/Screen Recording permissions,
and run it in bounded mode with a reviewed capability manifest.  The profile
allows only application/window observation and three input primitives.  It
excludes app launch/kill, clipboard, file transfer, JavaScript execution and
unrestricted browser-profile attachment.

Cua is a fallback for native security dialogs or virtual keyboards that
chrome-use cannot address.  A successful input call is not proof that a
keyboard-security product accepted the input; verify the resulting UI and
server-side login state.

## Nuclei Active status

The in-repo `web-nuclei` process now exposes an optional
`nuclei_active_query_probe`. Register the same endpoint as
`web-nuclei-active` using `deploy/mcp/finance/extensions.env.example`, and set
`active_enabled=true` only in the authorized engagement policy. It performs one
generated reflection probe against an exact allowlisted path/query key and
returns an observation, not an XSS finding.

Community/CVE templates remain disabled. Directly wrapping
`nuclei -u <agent-url>` would bypass target selection, path allowlists and
request budgets, so it remains prohibited.
