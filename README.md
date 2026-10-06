# Redteaming Agent

Evidence-backed, authorized-scope red-team orchestration core.

Read [SECURITY.md](SECURITY.md) before running the agent against any target.
The implementation map is in [docs/architecture.md](docs/architecture.md).

The first milestone is evidence-backed finding generation:

```text
asset discovery -> intent -> detector -> candidate -> verifier -> evidence gate -> finding -> report
```

This repository starts with a dependency-free domain core so the orchestration and finding rules can be tested before connecting MCP, HTTP, database, or LLM adapters.

Role handoffs are explicit: Planner creates a Scope-bound plan, Worker executes
one bounded recon or HTTP-surface intent, Verifier replays candidate evidence,
Publisher creates a Finding only after the evidence gate, and Reporter waits
for an explicit report request. The LLM cannot bypass Scope, execute arbitrary
commands, or publish a Finding directly.

When an LLM is used to prioritize hypotheses, it receives only the already
gated hypothesis metadata and aggregate observation counts/statuses. Endpoint
paths, identity headers, response bodies, and evidence identifiers are kept in
the local control plane.

For an explicitly approved provider/data-sharing setup, set
`REDTEAM_LLM_SEND_OBSERVATION_METADATA=true`. The prioritizer will then send
only bounded signal metadata (path, query-key names, status, header names, and
coarse indicators such as an error marker or inert reflection canary). Header
values, bodies, credentials, and Evidence IDs remain local. The default is
disabled.

Plans also carry declarative ATT&CK scenario metadata. The scenario registry
describes the assessment intent and technique references; it is not an exploit
script. External exposure and authenticated-web comparison are separate
scenarios, and the latter requires operator-supplied identity profiles.

Each plan is also materialized as an Objective Graph. Objectives have explicit
dependencies and lifecycle states; a worker cannot start a blocked objective,
and a completed non-reporting objective must carry evidence references. State
transitions are emitted into the execution timeline for auditability.

Nothing in the runtime contains a target URL, account, endpoint, credential, or fixed attack sequence. Those arrive through a `TaskSpec` and are selected at runtime. Detectors are plugins registered in `DetectorRegistry`; a task can select a subset or use all registered detectors. The included access-control detector is a generic rule over normalized observations, not a target-specific workflow.

The default passive HTTP specialists cover security headers, browser policy
headers (CSP, HSTS, Referrer-Policy, and Permissions-Policy when applicable),
CORS, error detail disclosure, session-cookie attributes, version-bearing server
headers, directory listings, OpenAPI authentication gaps, anonymous sensitive-field
exposure in JSON responses, and comparative access control. Each specialist creates a
hypothesis from observed evidence; the verifier replays only safe requests and
the publisher still requires a validated result. Additional specialist metadata
can be registered without changing the target or orchestration code.

The web surface also uses inert `redteam-canary-*` query values to detect
reflected input. A reflection candidate is never tested with an active payload;
it is published only when the same canary is reproduced by a second safe GET.

Recon uses a bounded Python standard-library TCP adapter. It checks the runtime Scope, probes a limited port range, and records discovered `host` and `service` assets plus evidence without requiring a system security tool.

The capability registry currently describes built-in port recon, DNSx, HTTPx, WhatWeb, Nuclei, ffuf, and Playwright. Skills select tools dynamically. Low-risk recon tools are available for authorized discovery; template scanning, content discovery, and browser workflows are marked as approval-required. Adding a tool means registering a `ToolSpec` and an adapter/parser, not embedding a target or attack path in the runtime. If a registered executable is missing, the capability gate emits a structured installation error and never falls back to an unapproved shell command.

Operator-authored skill metadata can be loaded with
`SkillRegistry.from_json(path, capability_registry)`. A manifest only declares
purpose, tools, completion criteria, and stop conditions; every named tool must
already exist in the capability registry, and execution remains Scope- and
approval-gated.

Execution is cross-platform: tool adapters produce an argument array and `ProcessRunner` invokes it with `shell=False`, so the same code works from PowerShell and Bash. Use `scripts/run_tests.ps1` on Windows or `scripts/run_tests.sh` on Bash-compatible environments. Shell rendering is used only for human-readable audit logs.

The interactive controller runs entirely from this repository's own `.venv`:

```powershell
cd D:\redteaming
.\scripts\setup.ps1
.\scripts\start.ps1
```

On Bash, use `./scripts/setup.sh` followed by `./scripts/start.sh`. The repository does not read another project, WSL, or an external virtual environment.

The setup scripts validate that the repository-local interpreter is still
usable. If a copied or moved workspace contains a stale `.venv` pointing at
an old absolute Python installation, the script recreates only that local
`.venv` before installing dependencies.

### Discovery policy (no target-specific constants)

The built-in baseline is bounded for safety, but discovery inputs are policy
configuration rather than target code. Copy `docs/policy.example.json` to
`.redteaming/policy.json` and edit the approved port set, seed paths, limits,
or same-origin/API discovery behavior. The CLI loads that file automatically;
set `REDTEAM_POLICY_FILE` to use another policy file. An exact `host:port`
Scope still takes precedence over a broad port list.

The optional top-level `detectors` list selects registered specialists for an
engagement; an empty list uses every registered detector.

HTTP surface discovery can expand from same-origin links, script resources,
robots directives, sitemap locations, and GET operations described by an API
specification, and same-origin `Link` response headers. Every discovered URL is normalized and checked against the
active Scope before it is probed; these sources do not authorize mutations.
The example seed paths are generic discovery hints, not an OWASP-only target
list and not a vulnerability allowlist. They can be replaced or extended by
the operator policy, while runtime observations remain the source of truth.
An empty `detectors` list means “all registered specialists”, not “disable
detectors”.
For services on nonstandard TLS ports, set `scheme: "https"` or `tls: true`
in the operator-provided service metadata; an unclassified service also gets
one bounded HTTPS retry after an HTTP connection failure.

## CLI command reference

After starting the agent, enter commands at the `redteam ›` prompt. Commands
starting with `/` are local control-plane actions and do not require an LLM
call unless noted.

| Command | What it does |
| --- | --- |
| `/help` | Show the command list. |
| `/scope HOST...` | Replace the current authorized Scope. Use an explicit host, URL, or host/port. |
| `/scope-add HOST...` | Add one or more authorized targets without replacing the current Scope. |
| `/status` | Show the current Scope. |
| `/doctor` | Check Python, optional dependencies, Docker, and registered external tools without probing a target. |
| `/recon` | Run the bounded pipeline: recon, HTTP surface discovery, detector hypotheses, replay verification, and Finding publication. |
| `/assets` | List normalized Assets discovered so far. |
| `/identity` | Optional advanced feature: register a session-only identity for owner/viewer or user/admin authorization comparisons. Not required for ordinary reconnaissance and passive checks. |
| `/identities` | List identity profile names currently held in memory. |
| `/report` | Print the current evidence-backed report. It does not create a file. |
| `/report reports/name.md` | Save the report to the specified Markdown path. |
| `/generate-report reports/name.md` | Explicitly save the current report to Markdown. Reports are never generated automatically. |
| `/evaluate category1,category2` | Compare validated Findings with supplied ground-truth categories. |
| `/evaluate cases.json` | Run owner-supplied repeatable evaluation cases. |
| `/connect` or `/onboard` | Open the LLM/OAuth connection menu. |
| `/model` | Show the active model. |
| `/model provider/model` | Change the model for the current session. |
| `/clear` | Clear the LLM conversation context; it does not delete discovered evidence or Findings. |
| `/quit` | Exit the CLI. |

Registering or replacing a Scope only changes authorization state; it never
starts planning, reconnaissance, detector analysis, or verification. Those
phases begin only after `/recon` or an equivalent explicit natural-language
assessment request.

The main workflow is:

```text
/scope approved.example:443
/identity
/recon
/assets
/generate-report reports/assessment.md
```

An explicitly requested authenticated authorization assessment is policy-gated: the agent requires
at least two operator-provided Identity profiles before it runs comparative
checks. If they are missing, it stops and asks for `/identity` instead of
silently treating an anonymous scan as an authorization assessment.

Natural language is also accepted for common actions, for example “승인된
https://approved.example을 등록하고 초기 정찰해줘”, “다른 승인 서버도
추가해줘”, “인증 사용자 권한을 비교해줘”, and “리포트 생성해줘”. The
“LiteLLM으로 연결해줘”, “현재 상태를 보여줘”, and “종료”. The internal
slash routes remain available for automation and backward compatibility, but
they are not required for normal interactive use.

For machine-readable output, ask “JSON 리포트 생성해줘” or provide a `.json`
report path. The JSON snapshot contains Scope, plan metadata, normalized
Assets, quarantined Evidence, Findings, graph state, and event history.

LiteLLM is installed in this repository's `.venv` and called directly. For reasoning models such as `gpt-5.5`, the client omits unsupported `temperature` parameters and enables LiteLLM parameter dropping.

The setup scripts create `.venv` and install the project plus LiteLLM. `/connect` configures the provider, model, and optional session-only API key. No secret is written by the CLI.

For repeatable local setup, copy `.env.example` to `.env` and fill only the
provider settings you are authorized to use. The CLI reads this repository-local
file without requiring `python-dotenv`; already-exported environment variables
always take precedence, and the file is ignored by Git. API keys entered in the
interactive menu remain session-only.

`/doctor` reports missing external capability executables as optional; it never
installs or invokes them implicitly.

Registered external tools pass through a single capability execution gate that
checks registration, explicit Scope targets, approval requirements, and
argv/executable matching before `shell=False` execution. URL/host-shaped
values embedded anywhere in argv are independently checked against Scope, so
an adapter cannot hide an additional out-of-scope target in a scanner flag.

Tool adapters then parse their output into normalized Assets and Evidence via
the same twin-graph path. Parsers are injected per tool; the control plane
does not contain a target-specific output format or vulnerability route. Each
adapter also stores bounded raw stdout/stderr as audit Evidence after masking
common credential-shaped fields. Tool output is marked as untrusted data,
wrapped with its origin, HTML-escaped before persistence, and checked for
prompt-injection markers; this metadata never becomes an instruction to the
planner or verifier.

HTTP response bodies use the same boundary: deterministic detectors receive a
bounded, credential-redacted analysis copy, while reportable Evidence also
retains a quarantined escaped copy and origin metadata.

The `chatgpt/<model>` route uses LiteLLM's native Responses API path, which is required for the existing ChatGPT OAuth provider. The ordinary `completion()` path is used for other providers.

The CLI also accepts natural-language connection requests before a Scope is registered. For example, `OAuth로 연결해줘`, `키 등록 없이 기존 설정으로 사용할래`, or `기존 LLM 설정 사용해` selects the local keyless provider profile. `/connect` provides the same choices as a menu.

Use `/connect` when you want a menu instead of remembering model names:

```text
1) 기존 OAuth 사용
2) API key 직접 입력
3) 현재 설정 유지
```

Natural-language requests such as `OAuth로 연결해줘`, `키 없이 연결해줘`, or `기존 설정 사용해` select the keyless provider profile. Provider and model selection are runtime settings.

`LiteLLM 연결 상태 확인해줘`는 모델을 호출하거나 비용을 발생시키지 않고
LiteLLM 설치 여부, 선택된 모델, 현재 세션/API key 또는 provider OAuth 설정의
사용 가능성만 표시합니다. 이 점검은 인증 성공을 보장하지 않으므로 실제
계획 요청 시 provider의 첫 응답에서 최종 인증이 확인됩니다.

For public distribution, use the bootstrap script. It creates the repository-local virtual environment and installs the package:

```powershell
.\scripts\bootstrap.ps1 -Launch
```

Provider credentials and model selection remain user-controlled and are never committed to GitHub. The built-in recon still requires an explicit authorized Scope before it can execute. Its default scan is bounded to TCP ports `1-1024` with short connection timeouts; broader scans and intrusive checks remain approval-gated capabilities.

HTTP probes also enforce the Scope request budget (`max_requests_per_minute`)
with a shared sliding-window limiter. When the budget is exhausted, the
request is skipped and a policy event is recorded instead of silently
continuing at a higher rate.

Candidate replay verification uses a bounded I/O worker pool capped by
`Scope.max_concurrency`; results are merged in hypothesis order so concurrency
does not change Finding or timeline ordering.

OpenAPI/Swagger documents are treated as inventory, not permission to execute
operations. Same-origin GET paths and inert query canaries are discovered, and
declared operation security schemes are retained on endpoint Assets. If an
endpoint declared protected returns a successful anonymous response, the API
authentication-gap specialist creates a candidate and verifies it with a
second safe request before publication.

External testbeds are a runtime integration surface, not a hardcoded part of
the agent. Any authorized HTTP/API/TCP service can be supplied through the
Scope and the tool adapter policy. The generic testbed launcher accepts an
image, published port, and container port at runtime; the agent image contains
no target name, host, port, or vulnerability path. See docs/testbeds.md for
optional training-testbed examples, safe loopback mappings, and the
repository-local fixture workflow.

For a fully containerized run, supply the target image and ports to the
generic launcher documented in docs/testbeds.md. Inside the agent container,
use the declared target service name and port as the explicit Scope. Keep
testbed ports bound to loopback unless the owner has approved another network
boundary.

## Authenticated comparison and evaluation

The HTTP pipeline can compare multiple operator-provided identities without
persisting credentials. In the CLI, use /identity once for each profile
(for example owner and viewer). Each profile may contain multiple headers
(Cookie, Authorization, CSRF, or a testbed-specific header). Header values are
entered with a hidden prompt and live only for the current process. During /recon, the agent
replays the same discovered endpoint for each profile; access-control findings
are published only when the differential response is reproduced.

The library equivalent constructs HttpIdentityProfile objects and passes the
tuple to runtime.run_http_assessment(scope, runs, identities=profiles). Header
values are never written by the runtime.

For a user-driven browser login handoff, install the optional browser extra
with `pip install -e ".[browser]"` and run `playwright install chromium`, then
use `/identity-browser <approved-login-url>`. The
operator completes the login in the visible browser; the agent captures only a
session Cookie header in memory for the current process. The URL is Scope-
checked first, and the session is never written to the engagement snapshot.

For repeatable testbed comparisons, label expected categories with
`EvaluationCase` or a JSON case file and score the validated output with
`EvaluationHarness`. The result exposes coverage, precision, verification rate,
and reproducibility rate. Ground-truth labels come from the testbed owner; the
LLM never invents them. See `docs/evaluation-schema.md`.

## Engagement control and reports

Reconnaissance is resumable within the current CLI session. Press Ctrl+C
during a run to pause it; completed port checks are checkpointed and the next
/recon continues with the remaining approved ports. This does not broaden the
Scope and does not persist credentials.

The CLI also writes a repository-local `.redteaming/engagement.json` snapshot
after each phase and when interrupted. It restores normalized Assets, redacted
Evidence, validated Findings, graph edges, event history, and the last plan on
the next launch. When the same Plan and Scope are resumed, completed Objective
Graph phases are skipped and marked `objective.resumed`; the existing plan is
reused for ordinary same-scope requests. A new plan is created only after an
explicit replan request or a changed scenario/Scope. Identity profiles and secret header values are
intentionally excluded, so an authorization comparison must be re-enrolled by
the operator.
Replacing the authorized Scope starts a separate engagement so assets and
Findings from a previous server are not silently mixed into the new report;
`/scope-add` is available when an additive multi-target engagement is intended.
Recon checkpoints for exact-port targets are isolated by `host:port`.
The snapshot uses an atomic temporary-file replacement and can be deleted when
starting a fresh local engagement.

Reports are never generated automatically after recon or detection. Request
one explicitly with /report, /generate-report, or natural language such as
generate report. Add a path to save Markdown locally, for example:

    /generate-report reports/engagement.md

Reports include the Plan's authorized Scope, scenario/ATT&CK metadata, graph
summary, execution timeline, Evidence references, and only validated Findings.
Validated Findings are grouped by host/service, vulnerability category, and
detector-provided root cause; multiple affected endpoints are retained under
one Finding when those keys match. Different services or root causes remain
separate.

PowerShell and Bash use the same repository-local setup flow:

    PowerShell: .\scripts\setup.ps1; .\scripts\start.ps1
    Bash:       ./scripts/setup.sh && ./scripts/start.sh

## Run tests

```powershell
python -m unittest discover -s tests -v
```

## Safety boundary

Only run against explicitly authorized targets. Tool adapters must enforce scope and approval before executing any action. A candidate is never published as a finding without verification evidence.
