# Shell workroom (TASK-003)

Stateful per-instance shell containers for the builtin in-process shell
provider (`src/redteam/capability/shell_*.py`). No MCP transport is
involved: the adapter speaks the internal `MCPAdapter` protocol and the
gateway serves the four `analysis.shell_*` tools like any other catalog
entry.

## Automatic CLI preparation

`redteam run` (including investigations started in the interactive CLI)
prepares the default Kali environment before assembling the runtime. It
checks Docker, starts an installed but stopped Docker Desktop on Windows,
waits for a Linux engine, then builds the image if necessary. If Docker is
not installed, it reports the official installation instructions rather
than downloading or installing it. First-use Desktop license, WSL or
permission prompts still need the operator; readiness has a bounded wait.

The prepared image is pinned to its immutable ID. A matching program
manifest is generated from that image and passed to discovery. Subsequent
runs reuse the prepared environment. Suspended runs record an environment
fingerprint and restore that exact image, manifest and Docker context;
missing cached records or images cause a continuity error rather than a
replacement build. Source updates do not silently upgrade this default.
There is no automatic image/cache cleanup or refresh command.

The managed image keeps `baseline` as its default. Set
`REDTEAM_SHELL_IMAGE_PROFILE=kali-default` to explicitly select Kali's
standard `kali-linux-default` metapackage profile. The broad profile is never
selected as a fallback. Both profiles install only while the image is built;
investigation runtime never performs an implicit pull or `apt` operation.
`profiles.json` maps each profile to its repository-owned package input, and
the same selected input is used by the Docker build and manifest generator.

`REDTEAM_SHELL_POLICY_PATH` selects an operator policy instead of the managed
profiles. A non-baseline managed profile and an explicit policy are mutually
exclusive. `REDTEAM_SHELL_AUTO_SETUP=false` keeps manual setup and suppresses
automatic Desktop startup/building. Legacy runs without a recorded
environment retain their original manual selection on resume.

`redteam validate` and `redteam tools list` do not start Desktop or build
images. Validation labels default preparation as pending execution rather
than claiming live readiness. Tool inspection shows explicitly configured
tools; the automatic default is assembled during investigation startup.
Automatic setup respects named Docker contexts and never starts a local
Desktop to repair an unavailable remote daemon. A raw `DOCKER_HOST`
override requires manual setup or an explicitly configured Docker context.

The workroom's existing resource limits, per-instance ownership, job
recovery and network admission remain in force. Image building requires
package downloads; this does not grant investigation scripts network
access. Target access requires the direct target route selected for the investigation.

## Hard invariants (do not relax here)

- Local execution creates containers with `--network none`. There is no
  networking switch in `ShellWorkroomPolicy`. Direct target execution uses the
  execution-admission extension to attach the selected bridge for one synchronous
  target run and detach it afterward.
  Without that explicit assembly, target and installation egress stay closed.
- Labels use `io.whs-redteam.shell-workroom.*`, disjoint from the
  execution MCP `io.whs-redteam.execution.*` labels, so the execution
  server orphan cleanup never matches a shell workroom.
- Containers keep `--cap-drop ALL` with only `SETUID`/`SETGID` (privilege
  drop to the policy uid/gid) and `KILL` (teardown signaling of demoted
  trees) added back as a confined launcher privilege: a root supervisor
  without `KILL` provably cannot signal a demoted child (`EPERM`), so
  the set is exactly what teardown needs and nothing more. Model
  children run demoted with zero effective capabilities, and adoption
  requires exactly this set. The base images ship setuid-root binaries
  (su, sudo, mount, ...), which no-new-privileges — set on every model
  child before exec — neutralizes.
- Scripts arrive on container stdin (`bash -s`, `python3 -`). The host
  never interpolates script text into a shell command line.
- Instance-installed programs stay inside that instance workroom and are
  never auto-promoted to the shared catalog.

## Build and pin

- `docker build -t shell-workroom:<tag> deploy/docker/shell-workroom`
- Push, resolve the digest, and record `repo@sha256:<digest>` as `image`
  in the shell policy file. The validator also accepts a local immutable
  image ID (`sha256:<64hex>`, directly usable by the Docker CLI) as a
  pinned reference for local verification without a registry push.
  `allow_unpinned_image` stays for genuinely unpinned dev use only, never
  production; local tests set it explicitly with a small image.
- `USER 65532:65532`, `WORKDIR /workroom`, `sleep infinity` as PID 1 so
  `docker exec` sessions share one warm filesystem per workroom key.

## Writable+exec surface

- `/workroom` is the one writable+exec surface: the image ships it owned by
  `65532:65532` (mode 1777), and the manager additionally best-effort
  `chmod 1777`s it after start so foreign/test images work too. It lives on
  the container filesystem, so binaries there execute and state survives
  `docker stop`/`docker start`.
- `/tmp` is a `noexec` tmpfs (sensitive area) and stays that way; never rely
  on executing from `/tmp`.
- Containers are created with `HOME=/workroom` and
  `PYTHONUSERBASE=/workroom/.local`, and `/workroom/.local/bin` leads
  `PATH`, so binaries an instance drops there resolve by bare name. Venv
  `bin` dirs are inventoried too but run by absolute path (explicit
  activation), never by guessing.
- On Kali Python, PEP 668 marks the interpreter externally managed, so
  `pip install --user` is rejected even with a writable HOME; the
  verified per-instance path is `python3 -m venv --system-site-packages`
  plus an offline (`--no-index`) install, which survives
  `docker stop`/`docker start` like everything else under `/workroom`.
- The network is still `--network none`, so only offline/local installs
  work until TASK-005.
- The baseline Kali image ships the FR-003 analysis set, installed FROM
  `packages.txt` (its single source: nmap, curl, jq, git, dnsutils,
  openssl, netcat-traditional, whois, tcpdump, sqlmap, wordlists, plus
  the provider mechanism deps procps/util-linux and python3-venv /
  python3-setuptools for the venv path). `program-manifest.json` is
  GENERATED from that same file crossed with live `dpkg-query` on the
  built image (`generate-manifest.py`), so versions, requested-to-
  installed resolution (for example `dnsutils` to `bind9-dnsutils`),
  per-command executables, and usable-mode notes never drift from what
  the image actually contains. The optional `kali-default` profile installs
  from `packages-kali-default.txt` and inventories the installed dependency
  closure of the Kali-owned metapackage, rather than pretending the
  metapackage name itself is an executable. Packages with no executable are
  retained with `inventory_status=no_executable`. Executable PATH resolution
  is recorded as `effective`, `shadowed`, or `off_path`; duplicate command/path
  identities or inconsistent status fail preparation. Verified usable modes under
  `--cap-drop ALL`: `nmap -sT` (connect scan; `-sS` needs `CAP_NET_RAW`
  and is unavailable), `tcpdump -r` file reads (live capture needs
  `CAP_NET_RAW` and is unavailable). Program execution checks here are
  LOCAL-ONLY (versions, loopback); replacement against real targets is
  TASK-005/008.

## Inventory and execution-state vocabulary

These states are deliberately separate:

- **installed**: the immutable image probe observed the owning dpkg package
  and executable file. It says nothing about networking or privileges.
- **route configured**: a local, target, or install route has provider
  configuration. `configured_unverified` still does not claim reachability.
- **mode supported**: the program metadata does not list the requested mode's
  required capability as unavailable. For example, installing `tcpdump` does
  not make live capture supported when `CAP_NET_RAW` is dropped.
- **execution admitted**: Control and the execution provider accepted one
  concrete invocation. Inventory and route metadata never grant this.
- **execution observed**: a bounded execution result/evidence record exists.
  It is the only one of these states based on an actual invocation.

`program.list`, `program.describe`, and the passive program-card sample all
project the same image/instance inventory. The passive sample is only a prompt
budget hint; paging and search continue across the complete inventory. Image
rows and per-workroom instance rows retain distinct owner scopes.

Managed preparation fingerprints profile name, package-input SHA-256,
immutable image ID, generated manifest SHA-256, Docker endpoint, and the full
runtime policy. Resume restores that exact identity and reports which safe
identity component drifted. The manifest contains no credential, host path, or
Docker-context value.

## Output caps (who wins)

- The shell adapter caps each stream at the policy `max_output_bytes`
  *during collection* (host memory stays bounded; `truncated` marks a
  reached cap).
- The gateway applies its own per-server serialized-output cut afterwards
  (default 1MiB, exceeded output is omitted with an error). The shell cap
  is the inner bound and normally wins because it is far smaller. The
  shell assembly additionally passes an explicit outer limit derived from
  the policy (two streams times worst-case JSON-escape headroom plus an
  envelope) through the gateway per-server output-limits seam, so a
  legitimate bounded result is never clipped by the outer cut first.

## Cancel and restart scope

- Every execution runs under a provisioned per-exec Python supervisor
  that launches as container root and drops the model child to the
  policy uid/gid with zero capabilities plus no-new-privileges before
  exec (verified live: `id`, `CapEff: 0`, `NoNewPrivs: 1`; any drop
  failure fails closed with no record, which the host reads as
  uncertainty). The supervisor owns the interpreter child in its own
  session, enforces the deadline (`TERM` then grace then `KILL` then
  reap), and writes the result to a root-only directory the script
  can neither read nor forge, so killing the supervisor fails and a
  forged or stale file can never pass as the outcome. Completion
  integrity: a missing record, an unprotected run, or non-empty
  unreaped survivors are uncertainty, never clean results. A `124`
  exit counts as a timeout only with no completed record, so a script
  that exits `124`/`137` by itself is never misreported, and a fast
  parent exit never masks a live background child (the supervisor
  reaps the whole tree first). Cancel signals that supervisor and then
  verifies nothing is left; a verified cancel frees its `max_jobs` slot
  immediately, while an unverified one holds `cleanup-unknown` until a
  deadline-confirmed supervisor lookup settles it (TTL alone never frees
  a possibly-live budget). Supervisor loss after execution may have
  started keeps the job `uncertain` (slot held, result flagged with the
  common uncertain-execution marker) until a supervisor-verified outcome
  or an authoritative container teardown settles it; a missing outcome
  with a running or unreadable container never settles.
  Detached children that escape session and tag together stay bounded by
  the in-container timeout; `--init` reaps zombies.
- A same-name container left by a dead process is adopted (started when
  stopped) only after ownership/policy verification: managed/context
  labels for the key plus live image, the expected network namespace,
  the confined launcher caps (`ALL` dropped, only `SETUID`, `SETGID`,
  and `KILL` added), `no-new-privileges`, user,
  workdir, and resource bounds (memory, cpus, pids, tmpfs size) must all
  match, otherwise reuse is rejected with
  `shell_workroom_adoption_rejected` before the foreign container is
  started, set up, or removed. `max_workrooms` is enforced against the
  label-filtered `docker ps -a` count, with the count-plus-create section
  serialized by a same-host OS file lock and fail-closed on unreadable
  daemon answers. Process-local job objects can be rebuilt from protected
  manifests, supervisor records, and bounded output spools after a restart.
  A recovered job-start reference returns the original submit acknowledgement;
  background completion and cancellation are read through job status. Missing
  or unverifiable execution records remain held and never authorize a rerun.

## Policy

See `deploy/shell-workroom/policy.example.json`. Copy it, set the pinned
image digest and operator limits, and hand it to
`shell_assembly.build_shell_entries`. When no policy is configured the
assembler skips the helper and the catalog honestly has no shell tools.

## Validate locally (no remote targets)

- `SHELL_TEST_IMAGE=python:3.11-slim pytest tests/integration/test_shell_workroom_docker.py -q`
- `SHELL_KALI_IMAGE=shell-workroom:t003-draft pytest tests/integration/test_shell_workroom_docker.py -q -k kali`
  runs the Kali-gated subset (offline program runs, manifest round-trip,
  venv/restart, foreign UID) against the built image.
- A freshly built image needs a freshly generated manifest. Do not disable
  `shell_image_manifest_mismatch` or edit only the digest in an old manifest.
  Generate the whole file from the actual local image using the existing
  generator's `--image` and `--output` options. `--offline` skips external
  mirror indices: installed package versions and executable paths are still
  observed from the image, but component origin stays unknown. It does not
  prove package integrity or origin. `--base-digest` records the build pin;
  it must come from the actual build record, not a guessed current tag.

  ```sh
  validation_dir=$(mktemp -d)
  python deploy/docker/shell-workroom/generate-manifest.py --offline \
    --image shell-workroom:t003-draft \
    --output "$validation_dir/program-manifest.json"
  SHELL_KALI_IMAGE=shell-workroom:t003-draft \
    SHELL_KALI_MANIFEST="$validation_dir/program-manifest.json" \
    pytest tests/integration/test_shell_workroom_docker.py -q -k kali
  ```

  The optional `SHELL_KALI_MANIFEST` changes only this integration test's
  explicit manifest input. Runtime catalog settings and image/digest
  verification are unchanged. Keep the default checked-in manifest when
  testing the image it was generated from.
- The isolation test inspects active interfaces, IPv4/IPv6 addresses and
  routes, not the number of dormant kernel tunnel devices. It sends no
  packets to external targets.
- Direct target execution does not require nftables, NFQUEUE or a sidecar image.
  Package installation routes are not part of the direct target connection.

## Assembly seam (catalog owner)

- `registry.replace_server_tools(SHELL_SERVER_ID, descriptors)`
- `adapters[SHELL_SERVER_ID] = adapter`
- `operation_bindings[str(tool_id)] = tuple(descriptor.operations)` per tool
- Full snippet lives in the `shell_assembly` module docstring.
