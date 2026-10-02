# Execution text-tools image

This image is the proposed, non-root runtime for the Execution Substrate MCP.
It installs only the declared text-processing packages on a digest-pinned
Debian slim base. The MCP policy still controls which executable paths may be
called; having another binary in the base filesystem does not add it to the
allowlist.

The `command_run` and `command_job_*` operations remain **pending agreement
(proposal)**. Building this image does not register or authorize them.

## Build

Run from the repository root under WSL2/Linux with Docker available:

```bash
docker build \
  --tag whs-redteam/execution-tools:local \
  deploy/docker/execution-tools
```

The base image digest and the versions of `coreutils`, `grep`, and `jq` are
pinned in the Dockerfile. A build intentionally fails if those exact versions
are no longer available from the configured Debian repository; update pins in
a reviewed change after checking the package changelogs.

## Record the locally built digest

```bash
IMAGE_ID=$(docker image inspect whs-redteam/execution-tools:local \
  --format '{{.Id}}')
docker tag whs-redteam/execution-tools:local \
  registry.example/whs-redteam/execution-tools:reviewed
docker push registry.example/whs-redteam/execution-tools:reviewed
docker image inspect registry.example/whs-redteam/execution-tools:reviewed \
  --format '{{index .RepoDigests 0}}'
```

Use the registry-produced `repository@sha256:<64 hex>` value in an operator's
local execution policy. Do not replace the placeholder in
`deploy/mcp/execution/policy.example.json` with a workstation-local image ID.
This repository does not push the image as part of tests.

## Verify the runtime user and tools

```bash
docker run --rm --entrypoint /usr/bin/id \
  whs-redteam/execution-tools:local
docker run --rm --entrypoint /usr/bin/jq \
  whs-redteam/execution-tools:local --version
```

