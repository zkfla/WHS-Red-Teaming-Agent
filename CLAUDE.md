# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

FinRedTeam is an authorized black-box red-teaming agent for financial web apps (primary
target: `vulnbank-target/`, a local Flask app). It implements a ReAct loop where an LLM
picks from a fixed action vocabulary each step; all verdicts on whether a finding is a real
vulnerability are computed by pure Python code (`oracles/`), never by the LLM. Design intent
and invariants are documented in `instruction.md` (原 spec) and `README.md` (current
architecture — README wins on the loop structure, since it was redesigned after the original
instruction.md pseudocode). Most prose docs in this repo (README.md, RUNNING.md,
DEBUGGING.md) are written in Korean.

## Commands

```bash
# one-time setup
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell
pip install -r requirements.txt
pip install -e .                  # enables `finredteam` / `finredteam-replay` / `finredteam-scorer`

# tests (no network required)
python -m pytest tests/ -v
python -m pytest tests/test_oracles.py -v          # single file
python -m pytest tests/test_oracles.py::test_name  # single test

# run modes
finredteam --smoke                       # bootstrap + manifest only, no LLM call
finredteam --fake-llm --max-rounds 10    # deterministic fake LLM, no API key needed — plumbing check only
finredteam --max-rounds 20               # real LLM, real exploration (needs ANTHROPIC_API_KEY or equivalent)
finredteam setup                         # build/approve an Engagement without running it
finredteam setup --fake-llm              # same, non-interactive

# post-run tooling
finredteam-replay latest                 # replay a stored trace with rich colors/tables, no re-execution
finredteam-replay <run_id> --step 5      # inspect one step in depth, including raw LLM call
finredteam-scorer runs/<run_id>          # score against ground_truth.yaml — fully separate from the agent
```

If not installed with `pip install -e .`, every command above has an equivalent
`python -m finredteam...` form (see README.md §5) — same code either way.

There is no configured linter/formatter (no ruff/black/mypy config in this repo) — match
existing style rather than introducing new tooling.

## Architecture

### The loop is ReAct, not a fixed pipeline

Each step: (1) code assembles an Observation (recent scratchpad, graph summary, pending
hypotheses, budget, memory summary), (2) exactly one LLM call returns
`{thought, action, action_input}`, (3) code dispatches the action — verdicts are always
computed by code, never the LLM, (4) result appended to scratchpad, loop back to (1). Entry
point: `finredteam/agent/react.py`. `agent/loop.py` is a thin backwards-compat shim that
delegates to `react.py`.

The LLM can only choose from 7 actions defined in `finredteam/planning/actions.py`:
`observe_graph`, `recall_memory`, `learn_baseline`, `discover_surface`,
`propose_hypothesis`, `execute_experiment`, `finish`. There is no free-text path/payload
input. Critically, **hypothesis and experiment are split**: `propose_hypothesis` only
validates against the known graph (no side effects); only `execute_experiment` actually
sends a request. `propose_hypothesis.target` must be a node id the graph already knows
(e.g. `"account:1234567890"`) — `planning/candidates.py::resolve_skeleton` validates
existence and returns an error string as the Observation if not, letting the LLM self-correct.

### Oracle selection is result-driven, not candidate-driven (important invariant)

When `execute_experiment` runs, **every** oracle registered in `oracles/registry.py::run_all`
executes unconditionally — the candidate's `operation` (`read_balance` vs `transfer`) has no
say in which oracles run. Each oracle independently decides UNKNOWN if it lacks the signal it
needs. This matters in practice: a single negative-transfer experiment fired both INV-1B
(sign inversion) and INV-6 (amount mismatch) simultaneously — a candidate-scoped design would
have caught only one. Do not reintroduce candidate-based oracle filtering.

`oracles/` is a pure-function layer: it must never import `llm/`, `probing/`, or `storage/`.
This is enforced by a source-inspection test, `tests/test_oracles.py::test_oracles_are_pure_no_llm_or_network_imports` — keep it that way when adding oracles.

### Engagement layer — Scope/Rules of Engagement gate everything

The CLI has no `--target`/`--engagement` flags. Running bare `finredteam` always starts (or
offers to reuse) an interactive Engagement Setup conversation before anything executes.
vuln-bank is not special-cased in code — it's just a pre-approved example committed at
`engagements/examples/vulnbank/`.

Flow: free-text description → LLM drafts structured fields (Target/Scope/allowed-forbidden
actions/rate & amount limits/sensitive data/cleanup/stop conditions), asking one clarifying
question at a time for anything ambiguous → **anything unmentioned defaults to the most
conservative (deny) value** — `engagement/setup.py::_draft_to_engagement` forces unset fields
to `False`/minimum, so the LLM structurally cannot widen its own scope → human must approve
(`approved_at` stamp) before anything is persisted or run → `engagement/compiler.py::compile_policy`
produces a `CompiledPolicy` = Engagement ∩ global safety ceiling (`config.py`) →
`probing/session.py::Session._request()` re-checks host/method/amount immediately before
every real network call (the only place real requests go out), and `agent/react.py`
pre-checks the same policy earlier to give the LLM a reason string instead of a crash. This
is deliberate defense in depth — don't remove either check.

Rule: an Engagement can never be looser than the global ceiling in `config.py`
(`MAX_AMOUNT`, `MAX_PROBES`, `MAX_CONCURRENT`, `MAX_REQUESTS_PER_SECOND`, ...). Allowed hosts
live only in the approved Engagement, never hardcoded in `config.py`.

`finredteam setup` runs the same conversation without executing afterward, for pre-staging.
Engagements built via `setup` are gitignored (may contain sensitive target info); only the
`engagements/examples/vulnbank/` example is committed.

### Module map

```
finredteam/
├── __main__.py, setup_cli.py     CLI entry — bare run = Engagement Setup + execute; setup subcommand; --smoke; --fake-llm
├── config.py                     global safety ceiling constants + LLMConfig/RunConfig (no allowed hosts here — see engagement/)
├── credentials.py                 interactive API key / OAuth token selection, saved to .finredteam/credentials.json (gitignored)
├── money.py                      Decimal-only Money/Currency — blocks float at the type level
├── engagement/                   scope/RoE layer: schema, setup (LLM-driven clarification), store,
│                                  loader, compiler (policy = engagement ∩ config.py ceiling), policy
│                                  (CompiledPolicy checks), cleanup, errors (PolicyViolation)
├── models/                       core (Verdict/Principal/Endpoint), state, finding (CandidateFinding —
│                                  no attack_class, uses operation instead), knowledge (Fact/Lesson/Entitlement)
├── llm/                          base (LLMClient protocol) + anthropic_/openai_/chatgpt_subscription
│                                  (codex-login subscription auth) + codex_auth + factory + fake
├── storage/                      db (JSONL, 16 collections incl. steps/llm_calls/created_resources/cleanup),
│                                  evidence (sensitive-data redaction), run_store (manifest.json)
├── targets/                      loader.py + configs/<name>.yaml — adapter config per target (base_url,
│                                  register/login endpoints), separate axis from Engagement
├── probing/                      session.py (★ policy enforcement + Decimal-safe HTTP — the only place
│                                  that makes real network calls) + executor; finance/ (state, burst/race,
│                                  card_fund, charge, loan/card_limit/bill (INV-5 policy_signals), auth/card_exposure, privesc);
│                                  generic/ (SQLi boolean-diff, JWT — wired via sqli_scan/jwt_forge probes)
├── recon/                        discovery, fingerprint (passive + 1-hop link extraction), entitlement, mapper
├── knowledge/                    graph (entitled_* / observed_* split, networkx), baseline (measured
│                                  ground truth for expected_delta), sensitive, inference
├── memory/                       working (short-term) / semantic (Fact) / episodic (per-operation attempt
│                                  history + Lesson) / summarizer (3k-token budget)
├── planning/                     actions.py (★ the 7-action ReAct schema), candidates.py (resolve_skeleton
│                                  validates operation+target, does NOT pick oracles), planner.py
│                                  (baseline-derived expected_delta), prompts.py, prioritizer.py
├── oracles/                      base, value (INV-1A/1B), amount (INV-6), idempotency (INV-4), concurrency
│                                  (INV-1C), authz (INV-2, needs graph), exposure (INV-9), privilege (INV-BFLA),
│                                  limit (INV-5 — 한도/정책, reads result.policy_signals; probe not wired yet),
│                                  generic (SQLi/XSS/BOLA/JWT), campaign (★ objective layer — composes findings
│                                  into adversary objectives OBJ-*, run_all_objectives), registry (★ run_all —
│                                  full oracle set, no candidate-based narrowing), verifier
├── agent/                        budget, phase, strategy (nudges only, no forced batching), termination
│                                  (Engagement policy_stop always wins over budget/saturation/convergence,
│                                  which always wins over the LLM's own finish request), react.py (★ main
│                                  loop + AgentContext + CLEANUP phase), loop.py (compat shim → react.py)
├── templates/attacks.yaml        concept-only reference; procedure/payload/endpoint are forbidden here
└── reporting/, evaluator/        report/transcript/repro/bugbounty; scorer (matches against ground_truth.yaml
                                   by invariant+endpoint, fully separate process from the agent)

engagements/examples/vulnbank/    pre-approved, committed Engagement for vuln-bank
vulnbank-target/                  the local target app itself (separate git repo, checked in as a directory)
```

### Known gaps (don't assume these exist)

- `resolve_skeleton` only implements `read_balance`/`transfer` operations. Idempotency
  (INV-4) and race (INV-1C) are reachable via dedicated `run_probe` probes
  (`charge_idempotency`, `race_transfer`), not via auto-generated `execute_experiment`
  candidates. Loan is now reachable via the `loan_limit` probe (INV-5); bill/card limit probes are still pending.
- INV-5 (limit/policy, `oracles/limit.py`) reads `result.policy_signals`, filled by
  limit probes, all wired into `run_probe`: `loan_limit` (F-10/F-11 — negative/zero loan +
  oversized-with-asserted-max), `card_limit` (F-17/F-18 — negative card limit on create and
  on update-limit, echoed in the response), `bill_limit` (F-21/F-23 — negative amount +
  below the biller's SERVER-declared minimum_amount; bounds come from the billers endpoint,
  not probe-asserted). Still NOT findable: F-19 (card limit not enforced) — deliberately
  deferred because no source endpoint clearly enforces the card limit on spend
  (bill payment checks balance, not limit; there is no `/pay` route), so a probe would risk
  a false-positive claim. F-19 stays labeled INV-5 (ceiling) with no probe.
- `probing/generic/*` (SQLi, JWT) IS now wired via `run_probe` probes `sqli_scan` and
  `jwt_forge` (this supersedes an earlier note that generic probes were unwired). G-XSS /
  G-BOLA oracles exist but have no dedicated probe yet.
- `discover_surface` findings land in the graph as facts but are not auto-chained into
  `propose_hypothesis`.
- `max_concurrency`, `allow_file_upload`, `allow_race_testing` in the Engagement schema
  compile but have no corresponding executable action yet. Only the `repeated_5xx` stop
  condition is actually wired into `agent/termination.py`; others (`latency_degradation`,
  `unexpected_sensitive_data`, `external_side_effect`) are stored but not auto-detected.
- OpenAI provider (`llm/openai_.py`) is untested against a live API key. The
  `chatgpt_subscription` provider works live but does not auto-refresh expired tokens
  (requires manual `codex login`).

## Non-negotiable invariants (per README §6 priority list)

- `money.py`'s Decimal-only `Money` type — never let floats represent currency.
- `entitled_*` / `observed_*` separation in the knowledge graph.
- `expected_delta.provenance` must always trace back to `knowledge/baseline.py` measured values.
- Verdicts are always computed by code (`oracles/verifier.py`), never by the LLM.
- Candidates never narrow which oracles run — the full registry always executes on
  `execute_experiment` (see "Oracle selection" above).
