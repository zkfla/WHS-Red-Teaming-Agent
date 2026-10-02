# Mobile/Analysis Track 0 Result

- Work ID: `CAPABILITY-MOBILE-ANALYSIS-TRACK0-20260929`
- Status: **complete** for installation, discovery, and static-analysis smoke tests
- Branch: `feat/capability-mobile-analysis`
- Base: `origin/capability` at `1ef55563483bc30fd3c0ef2faae852dca3ec000a`
- Integration rule: `dev` was not merged, fast-forwarded, or modified

## Completion checklist

- [x] Build the apktool MCP image with pinned upstream and protocol dependencies.
- [x] Discover apktool tools and validate the nine read-only allowlisted schemas.
- [x] Run `decode_apk` followed by `get_manifest` on InsecureBankv2.
- [x] Install local JADX 1.5.6 and jadx-ai-mcp plugin 6.4.1 in an isolated temp config.
- [x] Discover JADX tools, validate the nine read-only schemas, and call
  `get_android_manifest` against the APK loaded in JADX GUI.
- [x] Build mobile-next/mobile-mcp 1.0.4 into the Docker image on Node 22.
- [x] Discover 32 mobile-next tools and validate the four allowlisted schemas.
- [x] Register `apktool`, `jadx`, and `mobile-next` in an enabled AppConfig example.
- [x] Populate matching allowlists and operation bindings in `.env.example`.
- [x] Check host ADB and container reachability.

## Commits

| Commit | Result |
|---|---|
| `7ad053c` | Initial apktool image, clean stdio launcher, and environment example |
| `b7348e0` | Align apktool upstream workspace validation with the `/work` mount |
| `f79d4c6` | Include JADX in the generic live manifest drift check |
| `8416fe0` | Bake mobile-mcp 1.0.4 into a Node 22 image and fix Docker health |
| `1699f3b` | Add the enabled mobile/analysis AppConfig, environment links, and tests |

## Live evidence

The authorized training artifact came from the upstream InsecureBankv2
repository. The downloaded APK was 3,462,429 bytes with SHA-256
`B18AF2A0E44D7634BBCDF93664D9C78A2695E050393FCFBB5E8B91F902D194A4`.

apktool 2.9.3 decoded 7,196 files and returned the text manifest. The manifest
identified package `com.android.insecurebankv2`, 12 permissions,
`debuggable=true`, `allowBackup=true`, and multiple exported components.

JADX loaded 6,529 classes and 40,188 methods. The MCP bridge returned the same
package manifest through `get_android_manifest`.

mobile-next/mobile-mcp 1.0.4 advertised 32 tools. Only these four are bound:

- `mobile_launch_app` -> `app_launch`
- `mobile_get_device_logs` -> `response_analysis`
- `mobile_list_crashes` -> `response_analysis`
- `mobile_get_crash` -> `response_analysis`

The remaining 28 tools, including app install/uninstall and UI interaction,
stay outside the allowlist.

## Validation

- apktool live discovery: 16 tools, 9 allowlisted, no manifest drift
- JADX live discovery test: `1 passed`
- JADX-enabled unit selection: `38 passed, 2 skipped`
- mobile/analysis unit and fallback integration selection:
  `51 passed, 6 skipped`
- Ruff for the changed Python test: `All checks passed!`
- Docker Compose `tools` and `mobile` profiles rendered successfully
- mobile-next container reached Docker health `healthy`

The skipped tests are opt-in live-server or live-device cases. Each installed
external server's discovery was exercised separately in this work.

## Operational readiness

| Server | Ready now | Remaining operator preparation |
|---|---:|---|
| apktool | yes | Set an absolute authorized `APKTOOL_MCP_WORKDIR` |
| JADX | yes, local only | Keep JADX GUI open with an APK loaded; remove the optional ID otherwise |
| mobile-next | no device operations yet | Start/authorize an emulator or device and confirm `adb devices -l` |

Google Platform-Tools 37.0.1 was also tested from an isolated temp directory.
The ADB daemon started successfully, but `adb devices -l` returned no devices.
From the mobile-next image, `host.docker.internal:5037` was reachable while
container `127.0.0.1:5037` was not. Docker Desktop host-network HTTP forwarding
was not enabled on this machine, so the server catalog was verified over its
stdio transport. Enable Docker Desktop host networking for the documented HTTP
topology, or run the stdio transport on a host with ADB access.

Track A may proceed because Track 0's stated completion conditions are met.
The real-device dynamic E2E remains an opt-in Track B validation.
