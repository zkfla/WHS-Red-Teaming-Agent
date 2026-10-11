# Gated installation: runtime prerequisites

This sidecar is the existing provider's kernel network gate, not a new Agent
or a replacement for Control. Its image contains userspace nftables and
NetfilterQueue; it uses the Docker host's Linux kernel. Installing more
packages inside a container cannot enable a kernel option compiled out of
that host.

## Before the end-to-end proof

- A running Docker daemon and the already-local fixture images
  `shell-workroom:t003-draft`, `t005-gate-sidecar`, `python:3.11-slim`.
  The integration proof checks image availability before creating resources.
- Host support for nftables inet filtering, named counters, named quota,
  timeout sets, conntrack and NFQUEUE. In particular, `CONFIG_NFT_QUOTA`
  must be enabled (or its matching module available). Config presence alone
  is insufficient: the actual rule check must pass on the chosen daemon.
- The sidecar needs `NET_ADMIN` in the owned container network namespace.
  Do not give that capability to model scripts, use privileged containers,
  remove quota, or replace kernel enforcement with proxy environment variables.

Build from the repository root if these images are missing. These commands
download base images and packages; they are not offline commands:

```sh
docker build -t shell-workroom:t003-draft deploy/docker/shell-workroom
docker build -t t005-gate-sidecar -f deploy/docker/gate-sidecar/Dockerfile .
```

The following bounded diagnostic reuses the actual ruleset builder. It only
checks rules in a disposable network-none namespace, with no host mount,
packet daemon, grant reservation or target request. It never pulls an image:

```sh
./.venv/bin/python -c 'from redteam.capability.gated_window import build_ruleset; print(build_ruleset(queue_num=7, allowed=(), quota_bytes=4096))' |
  docker run --rm --pull=never --network none --cap-drop ALL --cap-add NET_ADMIN \
    --entrypoint nft -i t005-gate-sidecar --check -f -
```

Exit 0 confirms this rule check only, not proxy isolation, packet accounting
or recovery. Then run the actual local mock proofs:

```sh
./.venv/bin/pytest -q -rs tests/integration/test_gated_install_docker.py \
  tests/integration/test_gated_window_docker.py
```

These proofs use test-owned internal networks, a local synthetic package
mirror and proxy. They need no financial account, API key or ZAP/Burp key.
They do not prove live registry operation, package trust or financial testing.

## Observed local blocker (2026-10-11)

The tested Docker kernel was `6.12.54-linuxkit`, with
`# CONFIG_NFT_QUOTA is not set`. Default-drop and NFQUEUE check-only probes
passed, but named and anonymous quota probes failed with `No such file or
directory`. The production ruleset failed before attaching a transmit
network: installation was **not** verified. This is a measured limitation of
this Docker daemon, not a claim that every macOS Docker version lacks quota.

To finish this proof, select an approved Linux Docker host/VM whose kernel
supports the same rules. Cloud hosting is not mandatory. Another local VM
may suffice, but it must pass the checks; changing its kernel or the global
Docker context is an operator action, not an automatic code fix. Rebuilding
the userspace image or installing nftables on macOS does not repair the
observed missing kernel option.

Rule installation failure must stay fail-closed. Existing durable reservation
settlement retains the full charge and quarantines an unknown window. Do not
delete/reset the ledger, refund unknown usage or auto-repeat a real operation
to hide the failure; use the existing explicit reconciliation contract.

## Verified alternate local VM (2026-10-11)

A separate operator-prepared Lima VM (`whs-proxy`) uses Linux
`7.0.0-34-generic`, with `CONFIG_NFT_QUOTA=m` and the module loaded.
The existing ruleset check passed there, followed by the two Docker proof
files above: **5 passed, no skips**. The same local image IDs and current
uncommitted source were copied; neither the Mac Docker context nor production
policy was changed. Test TCP/UDP inputs now live under
`tests/integration/fixtures/gated_network.py`, not untracked `data/` files.

This proves the tested synthetic mirror/proxy and kernel boundary only.
It does not install a production proxy, register live mirror permissions,
authorize model-generated code on financial targets or validate a real LLM.
Keep Mac and VM venvs separate, omit credentials and existing run data when
copying, and run tests inside the VM so Docker bind sources are VM-local.
Recheck module/ruleset readiness after a VM reboot. Details and actual
settlements are in [implementation section 18](../../../docs/agent/dev-first-specialists-implementation-20261010.md#18-lima-linux-vm의-실제-설치-프록시-검증--2026-10-11).

Production additionally needs operator-provided pinned images, approved
mirror/proxy destinations and ports, scope and separate install/target
limits. Test pins and `allow_unpinned_image=True` are not deployment authority.

## Explicit sidecar selection

Set `sidecar_image` in the gated-network policy to an operator-reviewed
`repo@sha256:<digest>` or a local immutable `sha256:<image-id>`. Missing or
mutable selections are configuration errors; older files need an explicit
operator update. The runtime no longer selects `t005-gate-sidecar` for you.
It checks local availability before any grant reservation or workroom creation
and uses `docker create --pull=never`. Pinning fixes identity, not supply-chain
trust. The test image tags above are fixture lookup names; Docker proofs resolve
their actual local IDs into policy before opening a gate.

Use `redteam validate engagement.yaml --json` for offline policy/profile
validation. Exit 0 is not a Docker/kernel/MCP probe or execution admission.
Proxy origins must be credential-free HTTP(S) with no non-root path, query,
fragment or controls. Proxy pins are canonical IP literals and cannot overlap
target pins. See the [Web operator runbook](../../../docs/agent/web-operations-handoff.md)
for remaining operator inputs and the supported recovery/held boundaries.
