# Agent Runtime Reliability

The reasoning-mode Agent keeps the CI/CD parser, security fact graph and validator unchanged while
adding bounded runtime protections:

- A model turn is attempted at most three times. Only transient connection, timeout, rate-limit and
  server failures are retried. Authentication, permission, schema, context and validation failures
  fail immediately. LiteLLM's internal retries are disabled so attempts cannot multiply.
- Retry delays use bounded exponential full jitter and share the Agent's existing duration limit.
- The canonical Agent history remains intact. The model-facing view removes superseded limit
  messages and, only near the context limit, drops the oldest complete round groups. Exact facts
  must be fetched again through capabilities. `ListSecurityFacts` returns at most 100 relations per
  page and exposes `next_offset`.
- The Codex backend keeps one SDK runtime for the analysis but uses an ephemeral thread for each
  decision. It receives a bounded projection containing the trusted Agent policy, authoritative
  structured state, current result identifiers, progress counters and the seven newest Agent
  events. It does not maintain a second resumable conversation or receive the full canonical
  history. Omitted facts are re-queried through Capabilities.
- `checkpoint.json` is rewritten after every completed round and at terminal Agent states. It is
  the authoritative safe-resume snapshot of structured analysis state, working memory, cumulative
  limits and runtime counters; it is not a provider-owned model conversation. The file is written
  beside the final report through a same-directory temporary file followed by atomic replacement.
- `events.jsonl` is an append-only operational journal. It records bounded event metadata for
  attempts, rounds, model completions, capability requests/results and checkpoint commits without
  raw arguments, tool results or private chain-of-thought. A malformed final line is discarded;
  earlier corruption stops resume.
- `cicd-agent resume <run-dir>` resumes only at a committed round boundary. It restores cumulative
  round/token/cost/duration usage and runtime metrics, discards any incomplete round state/history,
  and increments the attempt ID. A per-run lease prevents concurrent resume. Completed runs,
  prompt-version drift, repository commit drift and dirty Git worktrees are rejected.
- Structured working memory is piggybacked on the already selected Capability, so it requires no
  extra model call or round. The underlying action signature excludes the patch, preventing note
  rewrites from bypassing repeated-call detection. Only semantic changes increment memory revision.
  The model-facing memory is capped at eight hypotheses and 12,000 serialized characters.

Runtime counters are also copied into `analysis.json` under `run_metadata.agent_runtime`. They
include model attempts, retries, accumulated retry delay, context compactions, omitted history
messages, checkpoint writes, no-progress rounds and the latest capability name. Progress is emitted once before and
after a round, plus once per retry, rather than continuously refreshing the terminal.

## Public implementations reviewed

The implementation was rewritten for this project using only the Python standard library. Design
and failure-policy comparisons used:

- [Tenacity](https://github.com/jd/tenacity): async retry composition, exception predicates and
  bounded stop/wait policies.
- [backoff](https://github.com/litl/backoff): full-jitter behavior and give-up predicates. The
  project is archived, so it was used only for comparison.
- [urllib3 Retry](https://github.com/urllib3/urllib3/blob/main/src/urllib3/util/retry.py): separation
  of retryable status/connection failures, bounded backoff and retry-after handling.
- [Google API Core retry](https://github.com/googleapis/python-api-core): predicate-driven retry
  under one total deadline.
- [OpenAI Python client](https://github.com/openai/openai-python/blob/main/src/openai/_base_client.py):
  retryable 408/409/429/5xx classes, bounded retry delay and jitter.
- [gRPC retry design](https://github.com/grpc/proposal/blob/master/A6-client-retries.md): maximum
  attempts and a deadline shared across attempts.
- [AWS backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/):
  full jitter for reducing synchronized retries.
- [OpenAI Agents Python sessions](https://github.com/openai/openai-agents-python/blob/main/docs/sessions/index.md):
  keeping stored history separate from the filtered model-input view.
- [OpenAI Codex SDK](https://learn.chatgpt.com/docs/codex-sdk): persistent threads for Codex-owned
  work, contrasted with the stateless LLM-adapter role used here.
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode):
  ephemeral session behavior and machine-readable structured output.
- [LangGraph persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx):
  checkpoint state separated from long-term cross-run memory.
- [CPython atomic bytecode writes](https://github.com/python/cpython/blob/main/Lib/importlib/_bootstrap_external.py):
  same-directory temporary output followed by `os.replace()`.
- [Rich progress](https://github.com/Textualize/rich): low-frequency progress/status reporting
  through the console dependency already supplied by hackingBuddyGPT.

No source from these projects is copied into the runtime modules.

Any new live Codex verification must explicitly select `gpt-5.6-luna`. Unit, contract, fake-LLM and
deterministic regression checks remain the default and do not consume model usage.

Before another model round, the runtime reserves at least the token count reported for the previous
round; it stops with `insufficient_token_reserve` when the remaining budget is smaller. Malformed
Codex structured Tool decisions are treated as recoverable provider output and retried under the
same bounded retry/deadline policy without logging the malformed content.
