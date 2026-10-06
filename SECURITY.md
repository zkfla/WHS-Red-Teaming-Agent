# Security and authorized use

Redteaming is intended only for assets that the operator owns or is explicitly
authorized to assess. Register the exact approved host, scheme, and port in
Scope before running reconnaissance. Keep training services on loopback or an
isolated lab network.

The control plane is designed to reduce accidental impact, but it cannot grant
authorization. Do not use the project against public targets, third-party
accounts, or infrastructure outside a written engagement.

## Secret handling

- Do not commit `.env`, API keys, cookies, browser profiles, reports, or
  `.redteaming` state files.
- Identity headers and browser session cookies are process-memory only.
- Evidence redacts common credential-shaped fields and marks target output as
  untrusted data.
- If a secret is accidentally captured, stop the engagement, rotate it, and
  remove it from local artifacts before sharing logs.

## Reporting a project issue

For a code or policy issue, include the affected module, a minimal local
reproduction, the exact Scope configuration shape (with secrets removed), and
the test output. Do not attach live credentials or data from an unauthorized
target.
