# Mobile Runtime Track B Protocol

- Work ID: `ORCH-AGENT-FEEDBACK-20260921 / Track B`
- Branch: `feat/capability-mobile-analysis`
- Base: `origin/capability`
- Authorized target: `com.android.insecurebankv2` only

## Scope

1. Add a config-supplied `DeviceTarget` without model-inferred device or package data.
2. Bind the configured mobile MCP device reference and package to mobile-next schemas.
3. Schedule only `mobile_launch_app`, `mobile_get_device_logs`,
   `mobile_list_crashes`, and `mobile_get_crash`.
4. Map package-specific fatal logs and crash reports through
   `Expectation -> Verdict -> Finding`, with Evidence and Attack Graph facts.
5. Verify deterministic logic with unit and scripted integration tests, then run an
   opt-in engagement against the authorized emulator.

## Guards

- Do not bind install, uninstall, tap, swipe, screenshot, batch command, or runtime
  instrumentation tools to canonical operations.
- Do not treat a successful launch, ordinary log line, malformed response, or another
  process's crash as a vulnerability.
- Do not infer the adb serial, MCP-native device reference, package name, or crash ID.
- Preserve Recon, Web, and static Analysis behavior.
- Do not merge, fast-forward, modify, or push `dev`.

## Completion checks

- Contract, binding, scope, graph identity, mapper, and Agent playbook unit tests pass.
- Scripted Agent-to-Finding E2E passes with a package-specific fatal signal and crash.
- Live mobile-next E2E proves launch and bounded diagnostics on the configured emulator.
- A strict live Finding is accepted only when the target actually emits a fatal log or
  owns a crash report; absence of that signal remains a recorded incomplete condition.
- Ruff and regression suites pass before commit.
