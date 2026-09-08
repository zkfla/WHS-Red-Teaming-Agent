# Source Baseline Receipt

Captured before rebuilding the generic workspace.

## Donor identities

- CI/CD: `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d`, clean after baseline run.
- Financial: `83885a478b9151ab4be652caff6ba564dce8ee6e`, clean after baseline run.
- Mobile: `f1ed305fb384de7e0043df8fc5e01f4935ab4689`, clean.

`uv` generated an untracked `uv.lock` while bootstrapping the Financial test environment. That generated file was removed immediately; no donor source file was changed.

## CI/CD focused baseline

Command:

```text
uv run pytest tests/unit/test_graph_frontier.py tests/unit/test_investigation_policy.py tests/unit/test_investigation_runtime.py tests/unit/test_security_candidates.py tests/unit/test_working_memory.py tests/unit/test_evidence_resolvers.py -ra
```

Result:

```text
85 passed in 17.44s
```

## Financial focused baseline

Command:

```text
uv run --with pytest python -m pytest tests/test_graph.py tests/test_oracles.py tests/test_execute_spine.py tests/test_concurrency.py tests/test_engagement_policy.py tests/test_policy_enforcement.py tests/test_session.py tests/test_termination_policy_stop.py -q
```

Result:

```text
89 passed in 30.79s
```

## Mobile characterization

The Mobile donor has no `tests/` directory. A one-off, donor-read-only characterization harness imported `tools/agent_tools.py` and exercised a temporary artifact tree.

Observed result:

```text
MOBILE_CHARACTERIZATION_PASS
```

Assertions verified:

- `..` root escape raises `ValueError`.
- `list_files(limit=999)` clamps to at most 200 results.
- `search_text(limit=999)` clamps to at most 100 results.
- files larger than 2,000,000 bytes are skipped by text search.
- `read_file(max_chars=999999)` clamps and emits `[TRUNCATED]`.

Static inspection also confirms the current Mobile system prompt explicitly states that preprocess security candidates are hints and must not restrict analysis scope, and that artifact natural language is untrusted data.

## Old generic reset inventory

Before Phase 1 reset:

```text
generic root: C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent-generic
src files:    117
src bytes:    2,108,086
tests files:  264
tests bytes:  2,355,258
```

Only the generic workspace `src/` and `tests/` trees are eligible for recursive removal in Phase 1. Donor paths are outside that deletion set.
