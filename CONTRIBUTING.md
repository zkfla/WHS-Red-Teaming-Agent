# Contributing

## Before you start

Read `ARCHITECTURE.md` and `docs/architecture/team-boundaries.md`. Find
your team's row, clone the repo, and start in your team's folder(s)
(see `README.md` "Repository Structure").

## Rules

- **`core` is read-only for you unless you're PM.** Need a new field,
  ID type, enum value, or Protocol method? Follow the Schema Change
  Process in `docs/architecture/team-boundaries.md` (Issue -> Proposal
  -> PM Review -> Merge). Don't edit `src/redteam/core/**` in a PR that
  also contains feature work — split it out.
- **Import `core`, not each other.** See
  `docs/architecture/dependency-rules.md`. If you find yourself writing
  `from redteam.control.service import DefaultControlPlane` inside
  `orchestration`, stop — you want
  `from redteam.core.ports.control_plane import ControlPlane` instead,
  wired up in `redteam.app.bootstrap`.
- **New concrete implementations get wired in exactly one place:**
  `redteam.app.bootstrap.build_runtime_deps`. Don't instantiate your
  service inside another team's module.
- **Tests are part of the change.** If you touch a `core` contract or
  port, update/add the matching test in `tests/contract` or
  `tests/unit`. If you touch the vertical slice (any node, the control
  pipeline, the gateway), `tests/integration/test_end_to_end.py` must
  keep passing.

## Workflow

1. Branch from `main`/`dev` per your team's convention.
2. `pip install -e ".[dev]"` and make sure `pytest -q`, `ruff check src tests examples`,
   and `mypy src/redteam` are clean before opening a PR.
3. Keep PRs scoped to one team's module (plus tests) — cross-team PRs
   slow down review and usually mean a boundary was crossed
   incorrectly.
4. If your PR touches `src/redteam/core/**`, tag PM as a required
   reviewer.

## What not to add at this stage

Per `instruction.md` section 21: no real exploit code, no payload
databases, no bulk auto-attack logic, no Graph DB / Kafka / Redis /
Kubernetes, no heavy web UI, no large batch of new MCP servers, no
second orchestration/agent framework alongside LangGraph. This is a
skeleton stage — see `ARCHITECTURE.md` for what's intentionally a stub.
