# Mobile Runtime Track B Result

- Work ID: `ORCH-AGENT-FEEDBACK-20260921 / Track B`
- Status: implementation complete; strict live Finding condition not observed
- Branch: `feat/capability-mobile-analysis`
- Base: `origin/capability`
- Protocol: [`mobile-runtime-track-b-protocol.md`](mobile-runtime-track-b-protocol.md)
- Result commit: recorded after the final validation commit

## Implemented

- Added `DeviceTarget` with three operator-configured identities:
  - `device_serial`: adb identity (`emulator-5554`)
  - `device_ref`: identifier accepted by mobile-next (`redteam_x86`)
  - `package_name`: authorized application (`com.android.insecurebankv2`)
- Seeded those values from engagement metadata and rejected partial configuration.
- Reused the linked asset ID for scope and Attack Graph identity.
- Bound mobile-next `device` and `packageName` from configuration-backed graph facts.
- Replaced the Mobile Agent's duplicate static-analysis step with:
  1. `mobile_launch_app`
  2. `mobile_get_device_logs`
  3. `mobile_list_crashes`
  4. conditional `mobile_get_crash`
- Registered exact result mappers for logs, crash inventory, and crash detail.
- Added bounded parsing and package ownership checks. A crash belonging to another
  process does not create a target fact, follow-up, or Finding.
- Added deterministic `android_runtime_crash` Finding generation from a confirmed
  package-specific fatal log or crash detail. Repeated evidence converges on one
  Finding ID.
- Kept install/uninstall, UI automation, screenshot, batch command, and runtime
  instrumentation tools outside the Agent playbook and canonical operation bindings.

## Automated verification

- B3 focused tests: `66 passed, 3 skipped`; Ruff passed after import ordering.
- Mobile runtime mapper tests plus static mapper/update regression: `12 passed`.
- Scripted full runtime E2E: `1 passed`.
  - Four actions executed through Agent, Control Plane, Gateway, Evidence, mapper,
    Attack Graph, and Finding.
  - Crash inventory produced `crash_id`; `mobile_get_crash` ran; fatal log and crash
    detail Evidence converged on one verified Finding.
- Device serial/MCP ref split regression: `64 passed`.
- Live mobile-next E2E: `1 passed in 34.13s`.
- Existing Recon/Web/static Analysis and orchestration integration regression:
  `82 passed`.
- Full unit and contract run: `1062 passed, 16 failed, 3 skipped`.
  - All 16 failures are pre-existing Windows compatibility failures in untouched
    artifact/session/proxy files: unavailable `os.O_NOFOLLOW`, symlink privilege
    `WinError 1314`, and POSIX mode assertions.
- `ruff check src tests`: `All checks passed!`

## Live emulator result

The originally named `redteam_test` AVD could not boot because its ARM image is not
supported by the installed QEMU2 emulator. `redteam_x86` also pointed at a missing
Android 30 image. The live run used the existing `redteam_x86` userdata with a
one-time `-sysdir` override to the installed Android 37.2 x86_64 image; the AVD config
was not rewritten.

The authorized APK was restored to that runnable userdata with adb test setup. Android
17 required clearing package-specific `review-required` permission flags before the
old targetSdk 22 app could reach `.LoginActivity`; no tap or other UI automation was
used.

mobile-next discovery returned `redteam_x86`, while adb returned `emulator-5554`.
This is why the two identifiers are distinct in `DeviceTarget` and configuration.

The live Agent run executed three successful actions:

1. `mobile_launch_app(device=redteam_x86, packageName=com.android.insecurebankv2)`
2. package-filtered `mobile_get_device_logs` (`No log entries received`)
3. `mobile_list_crashes`

The crash inventory contained repeated crashes for the system UWB service and none for
InsecureBankv2. The mapper correctly ignored them. Therefore no `crash_id`,
`mobile_get_crash`, or runtime Finding was produced in the live run.

Set `REDTEAM_TEST_MOBILE_E2E_EXPECT_CRASH=1` to make the opt-in live test require a
package-owned crash and verified Finding. Without that flag the live test still requires
the three real actions, successful Evidence, Attack Graph updates, and no false positive.

## Remaining condition

The strict live Definition of Done needs an authorized, deterministic InsecureBankv2
behavior that produces a package-owned fatal log or crash without install/uninstall,
UI automation, runtime instrumentation, or an availability-impacting forced crash.
The current launch-only behavior is stable and emits neither signal, so claiming a live
security Finding would be incorrect. The full Finding path is covered by the scripted
MCP E2E until such a target behavior is provided.
