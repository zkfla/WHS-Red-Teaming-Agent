# External testbeds

The agent is not coupled to a particular Docker image. A testbed is an
operator-selected, authorized target supplied at runtime. The same control
plane can assess a loopback service, a Docker-published service, a service
reachable from an approved lab network, or a remote engagement target.

## Safety boundary

Before adding a target, confirm ownership or written authorization, the
allowed host and port, the permitted testing window, authentication accounts,
rate limits, and excluded paths or assets. Keep intentionally vulnerable
training applications on loopback or an isolated lab network.

## Docker testbeds

PowerShell:

    .\scripts\start_testbed.ps1 -Image IMAGE -HostPort 3300 -ContainerPort 3000

Bash:

    ./scripts/start_testbed.sh IMAGE 3300 3000

Then register only the published endpoint:

    /scope http://127.0.0.1:3300

The image, host port, container port, and target name are runtime inputs. They
are not embedded in the agent or detector code.

For a containerized agent and target:

PowerShell:

    .\scripts\start_compose_testbed.ps1 -TargetImage IMAGE -HostPort 3300 -ContainerPort 3000

Bash:

    ./scripts/start_compose_testbed.sh IMAGE 3300 3000

When the agent runs inside Compose, use the target service name and declared
container port in the Scope, for example target:3000. Do not use localhost
from inside the agent container.

## Non-Docker targets

The same workflow applies to an approved staging URL, a local VM, a lab
cluster service, or a service started by another harness:

    /scope https://approved-staging.example
    /recon
    /generate-report reports/staging.md

Add another approved target without replacing the current Scope with:

    /scope-add https://second-approved-target.example

The control plane enforces Scope for every probe and only publishes a Finding
after a detector hypothesis has reproducible evidence.

For a web testbed that requires a real login, install the optional browser
extra, register the approved login URL in Scope, and use
`/identity-browser <approved-login-url>`. Complete the login manually in the
visible browser. Only the resulting Cookie header is held in memory for the
session; it is not checkpointed or included in reports.

## Adding another approved tool

Register a `ToolSpec`, implement a parser for that tool's output contract, and
run it through `CapabilityExecutor` and `CapabilityAdapter`. The executor
checks registration, explicit targets, Scope, approval, and argv before
execution. The adapter imports parser output into `AssetGraph`,
`EvidenceStore`, and `ExplorationGraph`; it never creates a Finding directly.
Parser output is untrusted: an Asset whose canonical value is outside the
active Scope is rejected before graph insertion and recorded as a policy event.
When a tool is invoked as part of a declared Skill, pass `skill_name`; the
runtime then verifies that the Skill permits that tool and emits auditable
skill execution events.
This keeps a new scanner, API client, VM helper, or cloud inventory adapter
independent from any particular target or vulnerability path.

For scanners that emit one JSON object per line, use the built-in
`JsonLinesAssetEvidenceParser`; it recognizes common URL, host, port, and
observation fields while still treating every record as untrusted Evidence.
This is an import adapter only: candidates still come from registered
detectors and Findings still require replay verification and the evidence gate.
