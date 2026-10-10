# Reviewed code artifacts — local author/reviewer contract

This feature produces and inspects **unexecuted code DATA**. It is not a
target executor, fixture runner, sandbox, authorization service or completion
authority. No generated source is imported, compiled or executed; `ast.parse`
checks syntax only. Existing approved shell/MCP tools retain their existing
Control/ROE boundaries; this helper grants them no new permissions.

## Selection and ownership

An explicitly selected specialist profile enables this optional feature by
binding **both** `code_generation` and `code_review` to different registered
role IDs. IDs are configurable; the engine does not branch on `code-author`
or a fixed seven-role list. Partial or same-role bindings fail configuration
validation. Profiles with neither binding remain compatible and do not gain
the helpers or an implicit artifact backend.

The author and reviewer use separate structured requests and trusted MD
bindings. They may use the same configured model; this is independent role
review, not proof of independent providers or a security certification.
The caller's normal/repair budget and the shared Run request ledger account
for both requests and every correction. SDK retries are disabled. Cancellation
after dispatch remains charged; unknown usage is not zero usage. An explicit
`unlimited_requests` Run policy does not remove the finite caller repair limit,
concurrency limits, stop checks, Task claim or Control constraints.

## Discoverable local helpers

| Helper | Required arguments | Output on success |
| --- | --- | --- |
| `code.prepare` | `objective`, `source_evidence_ids` | exact artifact evidence ID/digest, bounded Python program/cases as DATA, fixture/target-execution markers |
| `code.inspect` | `artifact_evidence_id` | the same DATA after current binding and byte-integrity checks; no model request |

The existing local-tool registry owns these handlers. Existing card/detail
lookup exposes their actual schemas and the native generic dispatcher executes
them. They are not additional remote MCP Tools or new target operations. The
code-author role is not schedulable as a target worker; Web/Mobile requesters
can ask the separately prompted author/reviewer to prepare an artifact.

## Inputs, records and race handling

Preparation requires the existing active Mission/Run and a RUNNING Task whose
claim and assigned role match the calling instance. It also requires explicitly
wired prompt and artifact ports and the caller decision budget.

Sources must be successful preserved TOOL originals from the same Run and
engagement, with proven action/invocation/tool/proposal and stored execution
origin in the Mission. Missing, failed, mock/fixture-marked, stale, invalid or
inconclusive sources are rejected before generation. Existing `unbound`
validity is compatible; it is not a claim that all real-world expiry or session
conditions are known. Source excerpts are bounded/redacted and treated as DATA;
their truncation is visible to both judgments.

The bundle binds Run/Mission/engagement/Task/instance, the Task completion
contract, delegated purpose, profile fingerprint, original content hashes and
author/reviewer prompt bindings. Review must echo the exact bundle digest.
Every model request and final read rechecks the captured inputs. Goal/input
changes, Task condition/claim changes, pending operator mutations, stop,
relevant validity/node changes (including ABA), and observed expiry reject
late output. An unrelated sibling observation or strategy-only revision does
not invalidate a non-strategy artifact. This reuses PlanningAdmission's
read-only check; it does not adopt a strategy or mutate a Task.

Validated bundles use the existing `core.ports.artifacts.ArtifactStore` port and
immutable filesystem backend. The composition root defaults to an `artifacts`
sidecar under its configured evidence directory only for the selected feature;
injection supports another compatible backend. Existing backend imports remain
compatible through `evidence.artifacts.base`. Hash verification, private file
permissions and reference confinement remain backend-owned.

An append-only SYSTEM EvidenceRecord of kind `reviewed_code_artifact_v1` binds
the exact bytes, review and sources. Identical output is content-addressed at
the record level; repeated preparation still spends its model requests but
does not append a second artifact record. Inspection reads by exact evidence
ID, not a caller-supplied path, and verifies original bytes/current bindings.
It never silently updates old records or treats an accepted review as a
Task/Goal completion, Finding or original target response.

Blob publication and evidence append are not a single filesystem/database
transaction. Interrupted publication can leave historical bytes or a record;
post-write inspection refuses changed/currently unproven bindings. No automatic
delete, refund, replay or target execution follows such a failure. This slice
does not implement orphan cleanup or complete crash recovery.

## Limits and unfinished work

- Python source: at most 32,768 characters; 1–16 synthetic JSON cases;
  whole bundle: at most 65,536 bytes; 1–8 source IDs. These are typed contract
  bounds, not target-specific action sequences or an AST security sandbox.
- Secret-shaped material detected by the shared redactor is rejected in the
  program. Redaction is not a guarantee of detecting every possible secret.
- Every result explicitly states `fixture_only=true`,
  `fixture_validation=not_run`, `target_execution_enabled=false`.
- Review assesses relevance/design; callable `evaluate`, stdlib-only behavior,
  sandbox isolation and synthetic case outcomes have **not** been executed or
  verified. Safe fixture execution is a separate remaining M5 slice.
- No actual LLM quality, Mobile/device behavior, financial vulnerability,
  target code execution or production release readiness is proven by the local
  scripted regression tests.

## Verification contacts

`tests/integration/test_code_artifacts.py` covers registry/detail/generic
dispatch, composition selection, original/linkage validation, independent
prompt/request accounting, finite corrections, no host execution, current
input/claim/ABA/stop races, immutable record replay, blob read/integrity,
historical publication and cancellation. Profile and existing artifact backend
tests retain configuration and storage-owner checks. The implementation report
records exact executed commands/counts and excluded environments.
