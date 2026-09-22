# Contract v0.1

The following are frozen as **Contract v0.1**, owned by PM:

```text
src/redteam/core/ids.py
src/redteam/core/enums.py
src/redteam/core/contracts/**
src/redteam/core/ports/**
```

"Frozen" means: no one outside PM edits these files directly. Everyone
else consumes them (`from redteam.core.contracts import ...`,
`from redteam.core.ports import ...`) and, within their own team's
modules, is free to build whatever they want against them (see
`docs/architecture/team-boundaries.md`).

`src/redteam/core/convert.py` and `src/redteam/core/config.py` are part
of the same frozen set — they're mechanical helpers over the contracts
above (no policy logic), but changing their signatures is still a
contract-shaped change or that other teams' code (see
`orchestration.nodes.execute`/`verify`, `evidence.service`) depends on.

## What "v0.1" signals

This is the first stable cut good enough for all three teams to build
against in parallel. It is **not** frozen forever — fields will be
added as real Agent/Control/Capability logic lands (e.g. `Hypothesis`/
`Finding` lifecycle fields, a real Policy Engine's rule schema). What's
frozen is the *process* for changing it, not the content.

## Change procedure

```text
Contract 변경 필요
    ↓
Issue 생성
    ↓
Contract Change Proposal 작성   (what field/type/Protocol changes, why, who's affected, migration plan)
    ↓
PM Review
    ↓
Contract 수정                   (PM merges the change to core/)
    ↓
영향받는 팀 코드 Migration        (each affected team updates their own module)
```

A Contract Change Proposal should say, at minimum:

- Which file(s) under the frozen set change, and how (new field,
  renamed field, new Protocol method, ...).
- Why — what real implementation work is blocked without it.
- Who's affected — which teams' code needs to change afterward.
- Whether it's backward-compatible (additive) or breaking (rename/
  removal/signature change) — breaking changes need a migration note
  for every affected call site, not just "it'll be fine."

Do not bundle a contract change into a PR that also contains your
team's feature work — split it so PM can review the contract change in
isolation (see `CONTRIBUTING.md`).

## Precedent from this pass

Two rounds of PM contract work already went through informal versions
of this process (see `ARCHITECTURE.md` section 4 "Resolved" for what
changed and why, e.g. `PolicyDecision.decision` -> `.verdict`,
`GraphNode.node_id`/`node_type` -> `.id`/`.type`,
`EvidenceStore.record` -> `.append`). Future contract changes should be
smaller and go through the Issue/Proposal flow above rather than a
repo-wide pass.
