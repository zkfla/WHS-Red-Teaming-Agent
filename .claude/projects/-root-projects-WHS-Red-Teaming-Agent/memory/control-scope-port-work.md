---
name: control-scope-port-work
description: Control scope port-aware matching work on dev-control; dev PR excludes test commits via cherry-pick
metadata:
  type: project
---

Control-team Scope fix (port-aware `host:port` matching + fail-safe hardening) lives on branch **dev-control**, in `src/redteam/control/scope.py` + `tests/unit/test_scope.py`. Design is the string `host:port` convention inside existing `allowed_hosts`/`denied_hosts` (no `core/contracts` change — final, see PLAN.md §5). See [[dev-prompt-architecture-rules]], [[control-team-context]].

**Team workflow decision (2026-10-02, PM preference): tests are kept OFF `dev`.**
- Code fix and tests go in **separate commits** on dev-control (so tests can be excluded).
- The `dev` PR is built by cherry-picking only the **fix** commits onto a branch off `origin/dev` — NOT by merging dev-control (a merge would drag the test commits in).
- Fix commits so far: `062b183` (port-aware), `14ee98b` (fail-safe), `9ce119c` (self-audit hardening). Test commits (exclude from dev): `b79853b`, `c5a894f`, `2dcdede`.
- dev-control itself keeps everything (fix + tests) = local regression safety net.

**Finish condition:** PR to dev only after teammate **B (Track B: audit/evidence regression verification)** completes. B works on dev-control too; handoff doc was shared (not committed). B must also keep code vs test commits separate.

**`git fetch`/push do not work from the Claude sandbox (no GitHub creds)** — the user runs push/PR from their own terminal (`gh` is NOT installed there either → PRs via GitHub web compare URL).

**Status 2026-10-02 EOD:**
- **Scope PR #20** (`pr/control-scope-port`): cherry-picked fix commits `062b183`+`14ee98b`+`9ce119c` onto dev HEAD `6978596`. All 3 teammate-reported scope issues (empty-port, malformed-deny-skip, port-omitted-URL) + self-audit fixes are in it. **CI is RED** — but only because dev itself was CI-broken (below), not the scope change.
- dev moved far ahead via a 224-file merge (HEAD `6978596`); several items from `docs/cross-team-final-collaboration.md` are now already done on dev (litellm default, LLM candidate wiring, Mission/Task/OperatorIntent, reconciliation→recovery, ArtifactBackend). scope.py on dev is still the OLD host-only version → scope PR cherry-picks cleanly.
- **CI-fix branch `fix/dev-ci-green`** (pushed + PR'd to dev): fixes dev's PRE-EXISTING CI breakage — `f384a9c` asyncio.timeout→wait_for (3.10), `15562f5` 34 CLI mypy errors (3.14), `1687453` mission flaky (`core/contracts/mission.py` validator now CLAMPS `updated_at` to `created_at` instead of raising; flaky was `test_terminal_mission_criteria[sqlite]`). Verified: full suite 1720 passed, ruff+mypy clean, flaky 20/20.
- **SEQUENCING to finish:** merge `fix/dev-ci-green` → dev (makes dev CI green) → THEN rebase scope PR #20 onto new dev → #20 goes green → merge.
- **Teammate B** now owns: Track B (audit/evidence regression verification) AND the dev-control/scope merge. User done for the day 2026-10-02.

See [[final-deliverable-not-demo]] — goal is real final deliverable + reliably-green CI, not a demo.
