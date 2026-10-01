# Development

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Needs Python 3.10+. A real LLM provider (`LiteLLMClient`) needs the `llm`
extra, or `agent` for that plus the SQLite checkpoint/work-ledger backend
`redteam run` always uses (`pip install -e ".[dev,agent]"` for a full local
dev setup in one command) — kept optional for its dependency footprint, not
a Python-version requirement; `litellm` installs and imports fine on 3.10
too (verified directly) — see `docs/adr/0002-litellm-gateway.md`.
Everything else, including all tests, runs fine on 3.10 with just `.[dev]`.

`redteam` loads a `.env` in the current directory into its own process
environment automatically at startup, best-effort
(`redteam.app.cli._shared.load_dotenv_if_present`) — a real shell/CI-exported
variable always wins over a `.env` value, so a manual `source .env` is
optional, not required. See `.env.example`.

## Running things

```bash
# Interactive: natural-language setup -> Preflight -> run
redteam

# Explicit/scripted, via CLI (prints every event)
python -m redteam.app.cli run examples/engagement.yaml

# Same thing, as a plain script
python examples/minimal_run.py
```

## Tests

```bash
pytest -q                    # everything
pytest tests/unit -q         # Scope/Event/Graph/MCP-adapter/dependency-rule/ID-enum/conversion/config/port-contract tests
pytest tests/contract -q     # Pydantic schema validation
pytest tests/integration -q  # full Agent -> Control -> Capability -> Evidence -> Event -> Graph flow
```

`tests/fakes/` holds test doubles for every `core` port — reach for
those (or `tests.fakes.build_test_application()`) instead of the real
`redteam.control`/`redteam.capability`/`redteam.evidence`
implementations when a test only needs *some* response, not a
correctly-evaluated one.

`pytest-asyncio` is configured with `asyncio_mode = "auto"`
(`pyproject.toml`) — `async def test_...` functions run without any
extra marker.

## Lint / types

```bash
ruff check src tests examples
ruff check --fix src tests examples   # autofix what's fixable
mypy src/redteam tests
```

`ruff` config: `[tool.ruff]` in `pyproject.toml` (line-length 100,
`E`/`F`/`I`/`UP`/`B` rule sets). `mypy` config: `[tool.mypy]`
(`ignore_missing_imports = true` since `langgraph`/`litellm` stubs are
incomplete; `disallow_untyped_defs = false` for now — consider
tightening once the skeleton fills in).

## Local data

`data/` (gitignored, the whole directory) is where the CLI's filesystem-backed
stores write runtime state when you run it: `data/evidence/<run_id>.jsonl`
(`FilesystemEvidenceStore`), `data/audit/<run_id>.jsonl`
(`FilesystemAuditStore`), `data/approvals/` (`FilesystemApprovalStore`),
`data/orchestration/` (the SQLite work-ledger/checkpoint, `agent` extra
runs), and `data/reports/<run_id>.json` (the persisted terminal report
`redteam status`/`findings`/`report` read back). All regenerated per run —
safe to delete between runs, never commit it.

## Docker / MCP servers

```bash
cd deploy/compose
docker compose up mcp-example
```

The container exposes a harmless `ping` tool at
`http://localhost:8765/mcp` over MCP Streamable HTTP. Use one of the
Web/API, Mobile, Recon, or Analysis adapters to exercise a real server.
See `deploy/mcp/README.md` for registration and deployment conventions.

Which servers are active comes from `REDTEAM_MCP_SERVERS` (a
comma-separated list of server ids), never from a config file; each
listed id's own settings are resolved from `REDTEAM_MCP_<SERVER_ID>_KIND`,
`_TRANSPORT`, and the corresponding HTTP `_ENDPOINT` or stdio `_COMMAND`.
Bootstrap fails fast when a configured server is missing settings, is
unreachable, or discovers no tools. `REDTEAM_MCP_SERVERS` unset or empty
is not an error: `run`/`tools list`/`tools health` get an honest empty
tool catalog and a `WARNING` on stderr, never a silent
`ExampleMCPAdapter` substitute. The literal value
`REDTEAM_MCP_SERVERS=example` is the one explicit, deliberate way to opt
into that in-process fixture instead.
