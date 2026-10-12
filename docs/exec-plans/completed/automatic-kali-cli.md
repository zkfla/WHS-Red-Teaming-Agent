# Automatic Kali preparation for CLI runs

## Status
COMPLETED

## Goal
CLI investigation startup prepares the default Kali Docker environment automatically. Missing Docker produces installation guidance; an installed but stopped Docker Desktop is started automatically, readiness is awaited, and the image is built/reused before execution.

## Context / Constraints
Work in the existing dev-orchestration checkout. Preserve unrelated .gitignore and scratch files. No merge, commit or push for this new change unless requested. Existing shell manager owns per-instance containers, admission, job recovery and isolation. Default automation must retain these contracts and network restrictions. Explicit shell policies take precedence. Read-only validate/tools commands must not build or launch Docker.

## Requirements
- FR-001: Detect Docker installation, support Windows Desktop installation locations, and give actionable official installation guidance without downloading/installing automatically.
- FR-002: Start stopped local Docker Desktop on Windows, wait with a bounded timeout and progress; report permission/WSL/engine failures. Respect remote/custom Docker contexts without starting an unrelated local engine.
- FR-003: Build the bundled Kali recipe on first investigation startup, cache by recipe content, resolve immutable image ID, reuse it on later runs. Keep the first prepared default pinned until explicit operator refresh; source updates must not silently replace an environment needed by suspended runs.
- FR-004: Wire the resulting default ShellWorkroomPolicy into run and resume; operator policy remains authoritative. Do not overwrite user settings or source files.
- FR-005: Generate a matching program manifest for the built image; discovery must receive that manifest, retaining its image-identity validation.
- FR-006: Cancel startup/build cleanly; concurrent preparations must not publish incomplete state. Existing per-instance recovery must not replay work or replace an existing container with a new image.
- NFR-001: Default preparation runs only at execution startup; offline inspection remains side-effect-free. Provide an explicit opt-out for deployments using other substrates.
- NFR-002: Repository and installed-package CLI both have access to one canonical recipe, without duplicated package lists.

## Confirmed Facts / Current System
run.build and build_resume_application call the pure build_shell_runtime(policy_path), which returns None when no policy exists. tools/validate rely on that pure contract. ShellWorkroomManager validates immutable image references and adoption identity. Image-program discovery currently defaults to a repository manifest and rejects a different image. Existing Dockerfile installs packages.txt, and generate-manifest.py can probe an image offline.

## Unknowns / Assumptions
Docker Desktop first-use license/WSL/admin prompts cannot be accepted by automation: bounded failure reports user action. Windows Desktop start CLI availability varies; use official command with installed-executable fallback. Actual Desktop-off testing must not stop existing user containers.

## External Research
Official Docker documentation supports docker desktop start and both system-wide and per-user Windows installation directories: https://docs.docker.com/reference/cli/docker/desktop/start/ and https://docs.docker.com/desktop/setup/install/windows-install/ . Prefer these paths instead of one developer's absolute installation path.

## Target System / Implementation Strategy
Capability owns async preparation and Docker lifecycle detection. CLI execution composition awaits preparation, then uses the existing shell runtime and gateway seams. Explicit policies keep their current contract. Default managed assets live in a user cache, content-addressed recipe tag and image-specific manifest. Bundle canonical deploy assets for installed distributions. Resume reuses persisted managed preparation rather than rebuilding just because source changed; verify existing container image/policy through the existing manager.

## Task DAG
T1 (root, SERIAL_ONLY): inspect contracts, plan review, finalize preparation API. Acceptance: source-owned responsibilities and resume/cache strategy agreed.
T2 (Muse worker, PARALLEL_AFTER T1): capability automatic preparation, assets packaging, image/cache/manifest; owns new preparation module, pyproject data files, tests for that module. API: async prepare_shell_policy(policy_path, *, progress) returning PreparedShellEnvironment(policy: ShellWorkroomPolicy, manifest_path: str | None). None policy_path means managed default. Explicit path validates and ensures its Docker is ready without building or changing image. Environment opt-out remains CLI responsibility. Acceptance: missing/stopped/ready/build/cache/cancel failure paths covered.
T3 (root, PARALLEL_AFTER T1): runtime/manager manifest injection, CLI run/resume integration, side-effect-free inspection and docs/CLI tests. Acceptance: prepared policy and manifest reach real runtime assembly, custom policy honored.
T4 (Sol 6.1 high independent reviewer, PARALLEL_AFTER T2,T3): correctness and boundaries review, no edits. Root evaluates findings.
T5 (root, SERIAL_ONLY): repairs, targeted tests/lint/types, actual Docker cached-image/workroom smoke without paid model calls or target scanning; complete plan.

## Test Strategy
Mock process boundaries to cover missing installation, Windows launch/readiness timeout, remote context, build failure/cancellation/cache hit, recipe changes and manifest identity. Integration tests prove CLI registration and failure propagation without live model calls. Actual Docker smoke verifies build reuse and confined local command execution; do not stop user's Desktop to manufacture a test.

## Progress
- Source and module instructions read; default wiring and manifest mismatch identified before implementation.
- T1 reviewed by Sol 6.1 high; four essential gaps accepted and task APIs updated.
- T3 CLI run/resume/checkpointless plumbing, manager manifest/context injection and optional durable environment identity implemented. Targeted CLI/legacy tests: 63 passed on Windows Python 3.14. Focused Ruff passed. Local mypy 2.4 internal error; final type validation will use Linux.
- DeepSeek high coordinates two Muse 1.3 xhigh workers: provider preparation and independent actual-dispatch resume regressions. Root retains integration and final judgment.

## Discoveries / Decision Log
- Keep build_shell_runtime(None) pure and disabled for inspection/backward-compatible programmatic callers; add execution-only preparation instead of changing all callers implicitly.
- Generate manifest from the actual image, never claim the checked-in manifest describes a newly built image.
- Default managed environment is sticky: publish immutable policy/manifest association once under a process lock and reuse it. Recipe hashes control build tags, not automatic upgrades of already-pinned runs. This keeps resume and parallel runs on the same default without introducing a new checkpoint schema.
- Independent Sol review corrected this continuity assumption: add one optional generic execution_environment_fingerprint to the existing durable component binding, omitted when None for legacy hashes. Provider stores immutable environment records by fingerprint. Recorded resumes restore exactly that record, never fresh-build on lost cache/image. Legacy resumes retain manual assembly and do not acquire a new default environment.
- Pin Docker context in the owner policy and compare context endpoint against the record. Preparation, manifest generation, and workroom execution use the same context. Raw DOCKER_HOST without explicit context requires manual setup; no synthetic TLS context.
- Use nonblocking OS lock attempts with cancellable async polling; cancellation cannot acquire a lock in an abandoned worker thread. Atomic publication follows all successful validation.

### Plan review (Sol 6.1 high)
Four findings: run continuity, isolated wheel assets, cancellation-safe lock ownership, Docker endpoint affinity. All accepted into the strategy and validation scope. Review was read-only; temporary log outside repo.

## Review Log / Validation Log
Sol 6.1 high performed plan, implementation, follow-up and final read-only reviews. Accepted findings: durable resume identity, installed-package assets, context affinity, context propagation into the gated daemon, conservative Desktop identification, and preparation descendant cleanup. All source fixes are implemented. The final review found no additional essential production issue; its missing test import was fixed.

Actual Docker validation built the bundled Kali image, discovered 429 programs, executed a confined local shell command including nmap discovery and Python output, restored the same preparation fingerprint and removed only its own workroom. A second fresh-cache smoke with the final Windows Job Object runner reused the image, regenerated/validated its manifest and passed the same execution check. Two separate processes restored the same environment without rebuilding. A non-editable wheel installed outside the source checkout supplied the canonical recipe assets and accepted injected manifest discovery. Desktop-off startup is covered by mocked lifecycle tests; the user's running Desktop was not stopped.

Final Linux full type/lint and targeted tests, plus Windows Python 3.10 targeted tests, are in progress. An initial Windows regression incorrectly required timeout when descendant pipe inheritance is absent; the corrected test checks descendant termination after parent exit on Windows and retains the timeout requirement on POSIX.

Full Linux mypy passed for 763 source files. First broad Linux targeted run: 229 passed, one Windows-mocked fixture lacked Docker executable discovery on Linux; the fixture now mocks that dependency. Windows Python 3.10 run: 63 passed. Additional cancellation review found a startup race before the Windows shim joins its Job Object: termination now kills the shim as well as job descendants, and process waits are bounded. Relevant final tests are being rerun after this fix.

Explicit operator policies are re-evaluated for new runs (including mutable development tags), while recorded resumes require their original policy and immutable image. Only the managed default uses the sticky active index.

## Requirement Traceability / Completion Criteria
FR-001..006, NFR-001..002 require source evidence plus relevant passing tests. No known essential defects; focused lint/type checks pass; actual Docker validation and mocked lifecycle validation clearly distinguished; documentation describes defaults, opt-out and first-use limitations.

## Requirement Status
- FR-001 / FR-002: `shell_preparation.py` owns installed executable discovery, endpoint validation, bounded readiness and hidden Desktop startup. Missing/stopped/ready/custom-engine cases are tested; actual stopped Desktop remains untested to preserve running user containers.
- FR-003 / FR-005 / NFR-002: `shell_image.py`, canonical generator arguments and setuptools data files build/reuse a content-addressed image and inject an image-matched manifest into the existing manager. Real build, installed-wheel assets and 429-program discovery passed.
- FR-004: run/resume composition and generic durable component binding preserve exact recorded environment identity; ordinary, cohort and checkpointless dispatch tests exercise forwarding. Explicit policy updates apply only to new runs, with original-record checks on resume.
- FR-006: preparation lock and atomic records prevent partial publication; process groups / Windows Job Objects own cancellation cleanup. Two-process cached preparation passed. Windows startup race fix kills an unjoined shim and bounded cleanup drains its pipes. Deterministic termination tests and live cancellation/parent-exit tests cover the change.
- NFR-001: pure runtime assembly remains available to tools/validate, and `REDTEAM_SHELL_AUTO_SETUP=false` keeps manual mode. Inspection and legacy compatibility tests passed.

## Important Decisions
Reuse the existing shell runtime, policy, manager and component binding. Add provider-owned preparation and one generic durable environment fingerprint because checkpoint hashes alone cannot represent the installed image/context/manifest. Preserve legacy resume behavior. Do not refresh sticky default environments silently. Keep external endpoint validation separate from shell admission and existing network policy.

## Remaining Risks
Docker Desktop first-use license, WSL or permission prompts still need user action. Stopped-Desktop launching is mocked rather than manufactured by stopping the user's daemon. Default cache/image loss intentionally blocks recorded resume until restored; no automatic replacement or replay. Image refresh/cache management is outside this request.

## Git Summary
The user subsequently requested committing and publishing this implementation to existing PR #32 on dev-orchestration, targeting dev. No merge is requested. User-owned .gitignore and pre-existing scratch files are excluded; worker-generated scratch was moved to a temporary archive outside the repository.

## Final Result
CLI execution now prepares Kali automatically, guides missing-Docker installation, starts an installed local Windows Desktop when appropriate, and builds/reuses the canonical image. Exact environment identity and program manifest reach the existing execution paths. All required completion checks passed; earlier pending validation entries describe intermediate runs and are superseded by the results below.

## Validation Summary
- Final Linux Python 3.14: full mypy passed (763 source files), full repository Ruff passed, relevant tests 231 passed.
- Windows Python 3.10: CLI/preparation/resume tests 63 passed before the final cleanup fix; affected image/preparation modules rerun after the fix: 44 passed in 15.20 seconds, no warnings. New CLI/dispatch tests also passed on Windows Python 3.14 earlier.
- Actual Docker: Kali image built successfully; confined local command exit 0, 429 programs discovered, same environment restored. Fresh-cache generation through the Windows Job runner passed. Two independent processes reused the recorded preparation without rebuilding. Installed non-editable wheel asset and discovery checks passed.
- Final git diff whitespace check passed. No paid LLM investigation or target scan was needed for execution validation.

## Independent Review Summary
Sol 6.1 high reviewed plan and implementation, including follow-up process ownership changes. Root accepted essential source-backed findings and verified fixes. Orchestrator independently checked the final cancellation race and bounded draining changes; no blocking issue remained.

## Issues Fixed During Review
Durable resume identity, installed wheel assets, context affinity including gated daemon, conservative Desktop detection, frozen dependency replacement, cancellation-safe locks, process descendants after parent exit, Windows shim startup cancellation race, and cross-platform test fixtures were fixed. Suggestions to replace bounded draining, change unreachable Windows-without-job fallback, or broaden cosmetic error normalization were not adopted: verified caller ownership and bounded cleanup satisfy the current contract without expanding scope.
