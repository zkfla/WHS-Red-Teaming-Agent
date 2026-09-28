# External Web MCP integration protocol

Work ID: `CAP-EXTERNAL-WEB-MCP-20260928`

## Goal

Add bounded integration profiles for the official ZAP MCP server, the official
Burp Suite MCP extension, chrome-use, and Cua Driver.  Define the separate work
needed for active Nuclei execution without weakening the existing target and
network policy.

This work only applies to systems covered by a written test engagement.  An
MCP connection is not authorization to scan a target.

## Baseline

- Keep the five-server synthetic finance lab unchanged.
- Preserve the existing HAR/ZAP snapshot and passive Nuclei services.
- Reuse the generic MCP Registry, allowlisted transport and canonical action
  bindings.
- Do not expose arbitrary shell, JavaScript evaluation, file upload, Burp
  configuration editing, raw target selection, or unrestricted scanner tools.

## Delivery phases

### Phase 1 — external MCP profiles and authentication compatibility

1. Support an exact `Authorization` header value selected through an
   environment-variable name.  This is required by the ZAP MCP add-on, which
   does not add or expect a Bearer prefix.
2. Add operator-run environment profiles for:
   - ZAP discovery and scan-control tools;
   - read-only Burp proxy history;
   - optional Burp Professional Collaborator tools;
   - chrome-use read/click/type tools;
   - Cua Driver window observation and login interaction tools.
3. Bind every exposed remote tool to an existing canonical operation or an
   empty operator-only binding.
4. Require Control approval for every mutating or credential-adjacent action.

Acceptance:

- secrets are absent from config objects, repr output, descriptors and docs;
- allowlists and operation maps have identical keys;
- ZAP can use a raw Authorization value while existing Bearer servers retain
  their current behaviour;
- settings, registry and adapter tests pass.

### Phase 2 — live discovery checks

Run only when the corresponding local product is installed.  Record upstream
version and the complete `tools/list` schema, then fail startup on allowlist or
schema drift.  Use a synthetic web target and test-only accounts.

ZAP and Burp remain bound to loopback.  chrome-use uses a dedicated Chrome
test profile.  Cua Driver uses a bounded capability manifest and a dedicated
desktop/VM where possible.

### Phase 3 — active Nuclei

Do not point the Nuclei process directly at an Agent-supplied URL.  Before
implementation, define:

- a pinned and reviewed template bundle with recorded hashes;
- template IDs, expected request count, supported protocol and risk tier;
- exact `target_ref`/path/query-key mapping;
- pinned DNS address plus preserved Host and TLS SNI;
- redirect, retry, OAST, headless, code-template and template-update policy;
- job ID, progress, cancellation and timeout behaviour;
- response and evidence redaction;
- a synthetic positive and negative fixture for every enabled template.

The first implemented profile is `reflected-query-marker`: one generated GET
against an exact allowlisted path/query key, connected to the already validated
IP while preserving Host and HTTPS SNI.  It disables redirect, retry, OAST and
updates and returns `observation_only`; reflection is not an XSS finding.

Community/CVE templates are still not enabled.  Before adding any, keep the
requirements above: pinned hashes, reviewed request counts and synthetic
positive/negative fixtures.  Code, headless, file, network and workflow
templates remain disabled.

## Non-goals

- automatic scope expansion from a spider or proxy history;
- unattended financial login, OTP, certificate or MFA handling;
- WAF or keyboard-security bypass;
- production scans, real transactions, race tests or rollback;
- treating a scanner alert as a verified Finding without an independent
  reproduction and the engagement's business oracle.
