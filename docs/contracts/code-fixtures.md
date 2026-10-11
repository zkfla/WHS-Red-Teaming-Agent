# Offline code fixture validation — M5 contract

This is an explicitly enabled, bounded **synthetic JSON test**, not target
execution, a new remote MCP server, or vulnerability/completion proof.
`code.prepare` still creates independently reviewed, unexecuted DATA.
`code.validate` separately tests that exact artifact through a configured port.

## Ownership and selection

- `core.ports.code_fixtures`: shared request/result/runner contract.
- `capability.code_fixtures`: adapter over the existing
  `ShellWorkroomPolicy` / `ShellWorkroomManager` supervised sandbox.
- `orchestration.code_fixture_validation`: current artifact admission,
  one-shot receipt and immutable result; no Docker imports or host execution.
- `app.bootstrap`: optional runner injection and policy loading. Both public
  assemblers use this same boundary; no additional scheduler or DI container.

The selected specialist profile must already bind independent `code_generation`
and `code_review` roles. The existing local-tool registry then provides
`code.prepare`, `code.inspect`, and `code.validate`. Validation takes only
`artifact_evidence_id`, never a source string, path, URL, interpreter or shell
command from the caller. Existing generic discovery/detail/native dispatch
consumes the registered schema.

The operator opts in with `orchestration.code_fixture_policy_path` or the typed
environment input `REDTEAM_CODE_FIXTURE_POLICY_PATH`. Relative paths are opened
relative to the launching process's current working directory; use an explicit
absolute policy path for repeatable assembly. This JSON file uses the existing
`ShellWorkroomPolicy` schema. Its image must be a content-pinned image with
Python 3 already installed locally, `allow_unpinned_image=false`, and
`default_timeout_seconds <= max_timeout_seconds`. No image installation/pull,
package installation, network policy or credential handling is added here.

Alternatively the composition root accepts `CodeFixtureRunner` injection for
an equivalent implementation or an explicitly synthetic test provider.
Injection plus a policy path is ambiguous and refused. Unselected code roles,
missing/invalid policy and unpinned image fail before Docker execution. An
unset policy/port means `fixture_runner_unconfigured`; there is no host fallback.
Existing ordinary dev shell/MCP permissions and engine are unchanged.

## Exact admission, isolation and outcome

1. Inspect the reviewed artifact, immutable blob bytes, author/reviewer prompt
   bindings, successful source originals and their current validity.
2. Bind Run/engagement/Mission/Task/instance, current claim, completion contract,
   delegated purpose, selected profile and mechanism/operator-policy fingerprint.
   Relevant source/validity references use existing PlanningAdmission CAS.
3. Atomically consume one admission identity in the existing coordinator. The
   transaction checks staged inputs only: no model/Docker/Evidence/Graph I/O and
   no nested coordinator call inside its change callback.
4. Run only the artifact's supplied cases in a separate fixture workroom. Its
   key derives from the admitted validation hash, never a target actor's warm
   workspace. `evaluate(input: dict)` must return a JSON object. Expected results
   stay **outside** the container and host-side canonical JSON comparison does
   not equate `true` with `1`.
5. Preserve an append-only SYSTEM `code_fixture_result_v1` record with exact
   binding/result digest. Recheck current inputs before returning it as current.
   A STOP during execution preserves history but refuses current admission.

The existing supervised manager enforces network-none, no host mounts,
bounded tmpfs/CPU/memory/PIDs/output and no-new-privileges. The container's
private writable layer/workroom is **not** a read-only root or a guaranteed
disk-byte quota. The supervisor retains only the capabilities needed for
demotion/teardown; its protected supervisor starts the untrusted Python child
as the configured non-root UID. The fixture adapter never attaches the gated
network extension or widens the language set. A pinned image is required, not
proof that the operator-selected image or Docker runtime is free of defects.

Requests are bounded to 32,768 source characters, 1–16 cases and 65,536 total
JSON bytes. Invalid JSON/non-finite numbers fail validation. Generated harness
size is checked against the existing script limit before a container is created.
Cases share the configured execution deadline and max-jobs bound. Cleanup
verifies the manager's label/image/isolation ownership before removing **only**
that fixture container. Unverified cleanup is held, never passed; foreign
containers are not removed. Cancellation propagates after bounded cleanup.
Hard process death may leave a container; its deterministic key is diagnostic,
not permission to rerun or delete unverified resources automatically.

| Result | Meaning | Retry/target meaning |
| --- | --- | --- |
| `passed` | Every supplied case compared equal and teardown was verified | Synthetic case agreement only; no Finding or Task/goal completion |
| `failed` | Known mismatch, execution/output error or deadline | Original failure preserved; no automatic rerun |
| `held` | Missing/unverified execution or teardown outcome | Never promoted to success or automatically redispatched |

Every result states `fixture_only=true` and `target_execution_enabled=false`.
Result DTOs contain bounded counts/reason codes, not raw source/stdout/stderr or
arbitrary Docker exception text. The stored reviewed artifact remains the
unexecuted generation record (`fixture_validation=not_run`); a later validation
does not rewrite its bytes, review, or original metadata.

## Recovery and compatibility

The one-shot admission is persisted by the existing atomic coordinator. A
duplicate concurrent call or resumed admission **without a matching original**
returns `fixture_original_missing_held`, not a second execution. A preserved
exact original can be read after a lost append acknowledgment/provider reopen,
including a failed/held original. Scope/source/blob/result corruption is refused.
This is not an exactly-once external target execution guarantee or claim handoff.

The fingerprint binds mechanism version plus validated operator policy to the
runtime component/planning/artifact identity. Changing image/limits/mechanism
cannot force an old artifact or Run to resume with new semantics. When this
optional feature is absent, the new null field is omitted from serialized
component bindings, preserving the legacy digest shape. No request/usage/claim,
Graph, approval, Mission or Task state is refunded or completed by validation.
The author/reviewer still use the shared LLM ledger; the fixture test itself
does not call a model.

## Role/Mobile readiness

`agent.readiness` and planner context expose the **same current role catalog and
eligibility projection** used by cards/detail/selection. The assembled Mobile
catalog's `all_descriptors`, `get`, and action lookup all apply the existing
vendor health/opt-in/approved asset-hash policy, without changing raw discovery.
No new vendor/role/server-ID branches are added.

`catalog_ready` means an eligible descriptor has observed HEALTHY status.
`activation_eligible` preserves dev's existing eligibility semantics: UNKNOWN
health may remain eligible but is not claimed healthy. The diagnostic carries
`target_execution_verified=false` and `execution_permission_granted=false`.
It performs no device/network/model call and grants no Control approval. Actual
approved device/app/session references and a permitted execution are still
required; missing ones are not guessed or manufactured.

## Verification / limits

Focused coverage: `test_code_fixture_runner.py`, `test_code_fixture_validation.py`,
`test_code_fixture_assembly.py`, and `test_specialist_readiness.py`. These use
explicit scripted models/runners; the persistence cases reopen real SQLite and
Filesystem Evidence providers. `test_code_fixture_docker.py` is opt-in with
`REDTEAM_CODE_FIXTURE_TEST_IMAGE` pointing to an already-local pinned image.
It checks actual supervised execution, UID/caps/no-new-privileges, no host write,
network-none denial, timeout and cancellation teardown.

These tests prove the bounded local fixture path, not malicious-code security
certification, real LLM quality, actual Mobile/financial target behavior, or the
separate dev long-lived shell/gated safety regressions. Actual generated-code
target execution remains disabled until its operation, target/path, request
limits, isolation and Control policy are explicitly agreed.
